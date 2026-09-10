"""Regression: September 2026 draft text was called verified despite broken structure."""

import json
from email.message import EmailMessage
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from apple_mail_fast_mcp.draft_inspection import (
    inspect_message_source,
    inspect_saved_message,
    read_saved_message_source,
)
from apple_mail_fast_mcp.mail_connector import AppleMailConnector
from apple_mail_fast_mcp.thread_inspection import get_scoped_thread


def test_regression_public_inspection_uses_registered_read_rate_tier():
    """Regression: live inspect_draft raised KeyError before reading any MIME.

    Exercise the real public rate gate while replacing only the native I/O.
    """
    from apple_mail_fast_mcp import server

    with patch.object(server, "inspect_saved_message", return_value={"success": True}) as read:
        result = server.inspect_draft("42", account="Work", mailbox="Drafts")
    assert result["success"] is True
    read.assert_called_once()


def _message(html: str, *, parent: bool = True) -> str:
    message = EmailMessage()
    message["From"] = "Chi <chi@example.test>"
    message["To"] = "Scott <scott@example.test>"
    message["Cc"] = "buyer@example.test"
    message["Subject"] = "Re: Nichias"
    message["Message-ID"] = "<draft@example.test>"
    if parent:
        message["In-Reply-To"] = "<parent@example.test>"
        message["References"] = "<earlier@example.test> <parent@example.test>"
    message.set_content("Hi Scott. Chi Ejimofor. Old message.")
    message.add_alternative(html, subtype="html")
    return message.as_string()


def test_regression_quoted_draft_text_is_not_verified_as_authored_reply():
    """Regression: reply and signature inside the iOS quote wrapper were called verified."""
    source = _message(
        '<blockquote><div>Hi Scott.</div><div id="AppleMailSignature">Chi Ejimofor</div></blockquote>'
    )
    result = inspect_message_source(
        source,
        expected_parent_rfc_id="parent@example.test",
        expected_body="Hi Scott.",
        expected_signature="Chi Ejimofor",
    )
    assert result["verification"]["status"] == "failed"
    assert result["verification"]["checks"]["authored_body"]["status"] == "failed"
    assert result["verification"]["checks"]["authored_body"]["present_in_quoted_region"] is True
    assert result["verification"]["checks"]["signature"]["status"] == "failed"
    assert result["structure"]["signature_regions"] == 0


def test_regression_subject_prefix_does_not_prove_reply_linkage():
    """Regression: changing Fwd to Re was mistaken for a verified native reply."""
    result = inspect_message_source(
        _message("<div>Hi Scott.</div>", parent=False), expected_parent_rfc_id="parent@example.test"
    )
    assert result["headers"]["Subject"] == "Re: Nichias"
    assert result["verification"]["checks"]["reply_linkage"]["status"] == "failed"
    assert result["threading"]["reply_headers_present"] is False


def test_regression_actual_outlook_signature_marker_retains_font_regions():
    """Regression: the user's live sent baseline used Outlook id=Signature, not AppleMailSignature."""
    html = '<div style="font-family: Aptos; font-size: 11pt">Hi Vendor.</div><div id="Signature" class="elementToProof" style="font-family: Aptos; font-size: 12pt">Chi Example<br>CICA Procurement</div>'
    result = inspect_message_source(
        _message(html),
        expected_body="Hi Vendor.",
        expected_signature="Chi Example CICA Procurement",
    )
    assert result["structure"]["signature_regions"] == 1
    assert result["verification"]["checks"]["signature"]["status"] == "passed"
    assert {
        font["value"]
        for font in result["structure"]["inline_fonts"]
        if font["region"] == "authored"
    } == {"Aptos", "11pt"}
    assert {
        font["value"]
        for font in result["structure"]["inline_fonts"]
        if font["region"] == "signature"
    } == {"Aptos", "12pt"}


def test_regression_quoted_outlook_signature_does_not_verify_current_signature():
    """Regression: prior-message Outlook signatures must remain within quoted evidence."""
    html = '<div>Hi Vendor.</div><blockquote><div id="sIgNaTuRe">Chi Example</div></blockquote>'
    result = inspect_message_source(_message(html), expected_signature="Chi Example")
    assert result["structure"]["signature_regions"] == 0
    assert result["verification"]["checks"]["signature"]["status"] == "failed"
    assert "Chi Example" in result["structure"]["quoted_text"]["text"]


def test_regression_outlook_sibling_history_is_not_authored_text():
    """Regression: live Outlook draft 242679's old reply was misclassified as new text."""
    html = (
        '<div style="font-family: Aptos; font-size: 11pt">Following up.</div>'
        '<div id="Signature">Current signature</div>'
        '<hr><div id="divRplyFwdMsg"><b>From:</b> Buyer</div>'
        '<div style="font-size: 19pt">Already sent text.</div>'
        '<div id="Signature">Earlier signature</div>'
    )
    result = inspect_message_source(
        _message(html), expected_body="Already sent text.", expected_signature="Earlier signature"
    )
    structure = result["structure"]
    assert structure["authored_text"]["text"].strip() == "Following up."
    assert structure["signature_text"]["text"].strip() == "Current signature"
    assert structure["signature_regions"] == 1
    assert "Already sent text." in structure["quoted_text"]["text"]
    assert result["verification"]["checks"]["authored_body"]["status"] == "failed"
    assert result["verification"]["checks"]["signature"]["status"] == "failed"
    assert all(
        font["region"] == "quoted" for font in structure["inline_fonts"] if font["value"] == "19pt"
    )


@pytest.mark.parametrize(
    "identifier", ["SignatureLogo", "SomeAppleMailSignature", "signature-footer"]
)
def test_regression_signature_detection_does_not_match_arbitrary_id_substrings(identifier):
    """Regression: signature matching needs exact markers instead of arbitrary substring matches."""
    result = inspect_message_source(
        _message(f'<div id="{identifier}">Not a signature region</div>')
    )
    assert result["structure"]["signature_regions"] == 0


def test_regression_actual_bad_draft_linkage_passes_while_authored_placement_fails():
    """Regression: live bad draft 242684 kept its RFC parent but quoted the entire new reply."""
    reply = "Hi Vendor. Please confirm whether you can source this product."
    result = inspect_message_source(
        _message(f'<blockquote type="cite"><div>{reply}</div><div>Chi Example</div></blockquote>'),
        expected_parent_rfc_id="parent@example.test",
        expected_body=reply,
    )
    assert result["verification"]["checks"]["reply_linkage"]["status"] == "passed"
    assert result["verification"]["checks"]["authored_body"]["status"] == "failed"
    assert result["structure"]["authored_text"]["text"].strip() == ""
    assert result["verification"]["status"] == "failed"


def test_regression_inspection_exposes_recipients_fonts_signature_quote_and_raw_html():
    """Regression: the old plain-content read hid recipients, fonts and quote structure."""
    source = _message(
        '<div style="font-family: Arial; font-size: 12pt">Hi Scott.</div><div id="AppleMailSignature">Chi Ejimofor</div><blockquote>Old message.</blockquote>'
    )
    result = inspect_message_source(
        source,
        expected_parent_rfc_id="<parent@example.test>",
        expected_body="Hi Scott.",
        expected_signature="Chi Ejimofor",
    )
    assert result["headers"]["To"] == "Scott <scott@example.test>"
    assert result["headers"]["Cc"] == "buyer@example.test"
    assert result["verification"]["status"] == "checks_passed"
    assert result["verification"]["rendered_ui_verified"] is False
    assert result["verification"]["matches_mail_font_preferences"] is None
    assert result["structure"]["inline_fonts"][0]["value"] == "Arial"
    assert result["structure"]["inline_fonts"][0]["region"] == "authored"
    assert result["structure"]["quote_regions"] == 1
    assert "blockquote" in result["body_html"]["text"]


def test_regression_inspection_without_expectations_does_not_claim_verified():
    """Regression: a successful read alone was reported as a verified draft."""
    result = inspect_message_source(_message("<div>Hi Scott.</div>"))
    assert result["verification"]["status"] == "incomplete"
    assert all(
        check["status"] == "not_requested" for check in result["verification"]["checks"].values()
    )


def test_regression_plain_text_cannot_establish_signature_or_quote_placement():
    """Regression: flattened Mail content hid whether the reply lived in a quote."""
    result = inspect_message_source(
        "To: a@example.test\n\nHi Scott. Chi Ejimofor",
        expected_body="Hi Scott.",
        expected_signature="Chi Ejimofor",
    )
    assert result["verification"]["checks"]["authored_body"]["status"] == "unavailable"
    assert result["verification"]["checks"]["signature"]["status"] == "unavailable"


def test_regression_attached_email_cannot_supply_current_reply_evidence():
    """Regression: inspecting the whole MIME tree must not verify an attached email as the draft."""
    outer = EmailMessage()
    outer.set_content("A draft with an email attached")
    inner = EmailMessage()
    inner.set_content('<div id="AppleMailSignature">Chi Ejimofor</div>', subtype="html")
    outer.add_attachment(inner)
    result = inspect_message_source(outer.as_string(), expected_signature="Chi Ejimofor")
    assert result["body_html"]["text"] == ""
    assert result["verification"]["checks"]["signature"]["status"] == "unavailable"


def test_regression_empty_source_is_failure_not_verified_empty_draft():
    """Regression: the helper's empty serialization result must remain an explicit failure."""
    result = inspect_message_source("")
    assert result["success"] is False
    assert result["error_type"] == "source_unavailable"


def test_regression_catalog_attachment_does_not_block_signature_inspection():
    """Regression: the actual E20 catalog exceeds the first inspector's 4 MiB cap."""
    message = EmailMessage()
    message.set_content("Hi Scott")
    message.add_alternative('<div id="AppleMailSignature">Chi Ejimofor</div>', subtype="html")
    message.add_attachment(
        b"x" * 4_400_000, maintype="application", subtype="pdf", filename="E20.pdf"
    )
    result = inspect_message_source(message.as_string(), expected_signature="Chi Ejimofor")
    assert result["success"] is True
    assert result["verification"]["checks"]["signature"]["status"] == "passed"
    assert len(json.dumps(result)) < 5000
    assert any(part["filename"] == "E20.pdf" for part in result["mime"]["parts"])


def test_regression_oversized_source_rejected_before_mime_parse(monkeypatch):
    """Regression: lifting the E20 source cap must preserve an explicit bounded failure."""
    import apple_mail_fast_mcp.draft_inspection as inspection

    monkeypatch.setattr(inspection, "_MAX_SOURCE_CHARS", 100)
    result = inspect_message_source("To: a@example.test\n\n" + "x" * 100)
    assert result["success"] is False
    assert result["error_type"] == "source_too_large"


def test_regression_inspection_reads_only_exact_saved_message_and_serializes_json():
    """Regression: the draft's numeric lookup scanned RFC IDs and exceeded Code Mode's deadline."""
    connector = AppleMailConnector(timeout=120)
    raw = {"id": "242684", "source": _message("<div>Hi Scott.</div>"), "source_complete": True}
    with patch.object(connector, "_run_applescript", return_value=json.dumps(raw)) as run:
        result = inspect_saved_message(connector, "242684", account="CICA", mailbox="Drafts")
    script = run.call_args.args[0]
    assert "repeat with acc in accounts" not in script
    assert "repeat with mb in mailboxes" not in script
    assert 'resolveMailbox(acc, "Drafts")' in script
    assert "source of msg" in script
    assert "whose (id is 242684)" in script
    assert 'message id is "242684"' not in script
    assert "NSJSONSerialization" in script
    assert connector.timeout == 120
    assert result["account"] == "CICA"
    assert result["mailbox"] == "Drafts"


def test_regression_unavailable_or_oversized_source_cannot_report_success():
    """Regression: an unavailable saved source must not be reported as verified."""
    connector = AppleMailConnector()
    with patch.object(
        connector,
        "_run_applescript",
        return_value='{"id":"42","source_complete":false,"source_characters":9999999}',
    ):
        result = inspect_saved_message(connector, "42", account="CICA", mailbox="Drafts")
    assert result["success"] is False
    assert result["error_type"] == "source_incomplete"


def test_regression_source_read_rejects_missing_scope_before_any_applescript():
    """Regression: omitted source context previously triggered cross-account scans."""
    connector = AppleMailConnector()
    with (
        patch.object(connector, "_run_applescript") as run,
        pytest.raises(ValueError, match="exact account"),
    ):
        read_saved_message_source(connector, "42", account="", mailbox="Drafts")
    run.assert_not_called()


def _thread_row(message_id: str, rfc_id: str, parent: str = "") -> dict[str, Any]:
    return {
        "id": message_id,
        "rfc_message_id": rfc_id,
        "subject": "Re: Nichias",
        "mailbox": "Drafts",
        "headers_raw": f"Message-ID: <{rfc_id}>\nIn-Reply-To: <{parent}>\nTo: scott@example.test\nDate: Thu, 10 Sep 2026 12:00:00 -0700\n\n",
    }


def test_regression_scoped_thread_keeps_rfc_evidence_and_marks_global_completeness_unknown():
    """Regression: thread reads dropped RFC links and presented subject-filtered output as complete."""
    anchor = _thread_row("42", "draft@example.test", "parent@example.test")
    candidates = {
        "candidates": [
            _thread_row("41", "parent@example.test"),
            _thread_row("40", "unrelated@example.test"),
        ],
        "errors": [],
        "finished_mailboxes": ["Drafts", "Archive/Vendors/Piping"],
        "hit_limit": False,
        "deadline_reached": False,
    }
    connector = AppleMailConnector(timeout=120)
    with patch.object(
        connector, "_run_applescript", side_effect=[json.dumps(anchor), json.dumps(candidates)]
    ) as run:
        result = get_scoped_thread(
            connector,
            "42",
            account="CICA",
            mailbox="Drafts",
            search_mailboxes=["Archive/Vendors/Piping"],
        )
    assert {row["id"] for row in result["thread"]} == {"42", "41"}
    assert result["complete"] is False
    assert result["scope_complete"] is True
    assert result["thread"][0]["in_reply_to"] == "parent@example.test"
    assert result["thread"][0]["account"] == "CICA"
    assert all("repeat with acc in accounts" not in call.args[0] for call in run.call_args_list)
    assert "whose (id is 42)" in run.call_args_list[0].args[0]
    assert 'message id is "42"' not in run.call_args_list[0].args[0]
    assert all(
        "repeat with mb in mailboxes of acc" not in call.args[0] for call in run.call_args_list
    )
    assert "inspectionStarted" in run.call_args_list[1].args[0]
    assert connector.timeout == 120


def test_regression_scoped_thread_timeout_returns_anchor_and_explicit_partial_status():
    """Regression: get_thread exceeded the 60-second tool deadline without useful partial evidence."""
    connector = AppleMailConnector()
    with patch.object(
        connector,
        "_run_applescript",
        side_effect=[
            json.dumps(_thread_row("42", "draft@example.test")),
            TimeoutError("native timeout"),
        ],
    ):
        result = get_scoped_thread(connector, "42", account="CICA", mailbox="Drafts")
    assert result["count"] == 1
    assert result["scope_complete"] is False
    assert result["errors"][0]["stage"] == "candidate_read"


def test_regression_scoped_thread_candidate_cap_is_reported():
    """Regression: limiting a thread scan must not silently report a complete conversation."""
    connector = AppleMailConnector()
    response = {
        "candidates": [],
        "errors": [],
        "finished_mailboxes": [],
        "hit_limit": True,
        "deadline_reached": False,
    }
    with patch.object(
        connector,
        "_run_applescript",
        side_effect=[json.dumps(_thread_row("42", "draft@example.test")), json.dumps(response)],
    ):
        result = get_scoped_thread(connector, "42", account="CICA", mailbox="Drafts", limit=1)
    assert result["candidate_limit_reached"] is True
    assert result["scope_complete"] is False


def test_regression_thread_chronology_compares_rfc_dates_in_one_timezone():
    """Regression: vendor and buyer local clock strings do not sort chronologically."""
    anchor = _thread_row("42", "draft@example.test", "parent@example.test")
    parent = _thread_row("41", "parent@example.test")
    parent["headers_raw"] = parent["headers_raw"].replace("12:00:00 -0700", "14:00:00 -0400")
    response = {"candidates": [parent], "errors": [], "finished_mailboxes": ["Drafts"]}
    connector = AppleMailConnector()
    with patch.object(
        connector, "_run_applescript", side_effect=[json.dumps(anchor), json.dumps(response)]
    ):
        result = get_scoped_thread(connector, "42", account="CICA", mailbox="Drafts")
    assert [row["id"] for row in result["thread"]] == ["41", "42"]


def test_regression_native_localized_headers_sort_by_mail_date_timestamp():
    """Regression: live all-headers Date strings were localized, not RFC 5322 dates."""
    anchor = _thread_row("42", "draft@example.test", "parent@example.test")
    parent = _thread_row("41", "parent@example.test")
    for row, timestamp in [(anchor, 1789070636), (parent, 1789070000)]:
        row["headers_raw"] = row["headers_raw"].replace(
            "Thu, 10 Sep 2026 12:00:00 -0700", "September 10, 2026 at 1:03:56 PM MST"
        )
        row["date_received_timestamp"] = timestamp
    response = {"candidates": [parent], "errors": [], "finished_mailboxes": ["Drafts"]}
    connector = AppleMailConnector()
    with patch.object(
        connector, "_run_applescript", side_effect=[json.dumps(anchor), json.dumps(response)]
    ) as run:
        result = get_scoped_thread(connector, "42", account="CICA", mailbox="Drafts")
    assert [row["id"] for row in result["thread"]] == ["41", "42"]
    assert result["chronology_complete"] is True
    assert all("my mailDateTimestamp(date received" in call.args[0] for call in run.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("mime_type", [None, "", "missing", "text/plain-unknown"])
async def test_regression_unknown_mime_update_cannot_reconstruct_original(
    monkeypatch, tmp_path, mime_type
):
    """Regression: absent source type must not authorize a potentially lossy rebuild."""
    from apple_mail_fast_mcp import server

    fake_mail = MagicMock()
    state = {
        "subject": "test",
        "body": "signature and original content",
        "to": ["a@example.test"],
        "attachment_names": [],
        "in_reply_to": "",
    }
    if mime_type != "missing":
        state["mime_content_type"] = mime_type
    fake_mail.get_draft_state.return_value = state
    monkeypatch.setattr(server, "mail", fake_mail)
    monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
    result = await server.update_draft("123", body="new")
    assert result["error_type"] == "native_update_required"
    fake_mail.create_draft.assert_not_called()
    fake_mail.delete_draft.assert_not_called()
