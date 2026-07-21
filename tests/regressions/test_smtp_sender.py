"""Unit tests for the SMTP submission transport (issue #322).

The mock boundary is ``smtplib`` — the SMTP equivalent of the connector's
``_run_applescript`` / ``IMAPClient`` boundaries. No test opens a real
socket or touches real credentials.
"""

from email import message_from_bytes
from email.policy import default as _default_policy
from unittest.mock import MagicMock, patch

from apple_mail_fast_mcp.smtp_sender import SmtpSender


def _raw_with_bcc() -> bytes:
    return (
        b"From: Fred <fred@example.com>\r\n"
        b"To: alice@example.net\r\n"
        b"Cc: carol@example.org\r\n"
        b"Bcc: secret@example.com\r\n"
        b"Subject: Hi\r\n"
        b"\r\n"
        b"Body text.\r\n"
    )


class TestStartTls:
    @patch("apple_mail_fast_mcp.smtp_sender.smtplib.SMTP")
    def test_envelope_from_is_message_from_not_login_and_recipients_explicit(
        self, mock_smtp: MagicMock
    ) -> None:
        """Regression for #322 / PR #404: the envelope ``MAIL FROM`` is the
        message's own ``From:`` address (``fred@example.com``), NOT the SMTP
        AUTH login (``u@example.com``). Reusing the login as the envelope
        sender is what made custom-domain iCloud reject the send with
        ``550 5.7.0 From address is not one of your addresses``. ``RCPT TO``
        is the caller's explicit recipient list.
        """
        client = mock_smtp.return_value.__enter__.return_value
        sender = SmtpSender("smtp.example.com", 587, "u@example.com", "pw")
        recipients = ["alice@example.net", "carol@example.org", "secret@example.com"]

        sender.send(_raw_with_bcc(), recipients)

        # AUTH login and envelope-from are two distinct values.
        client.login.assert_called_once_with("u@example.com", "pw")
        kwargs = client.send_message.call_args.kwargs
        assert kwargs["from_addr"] == "fred@example.com"
        assert kwargs["from_addr"] != "u@example.com"
        assert kwargs["to_addrs"] == recipients


class TestValidation:
    @patch("apple_mail_fast_mcp.smtp_sender.smtplib.SMTP")
    def test_wire_message_is_parseable(self, mock_smtp: MagicMock) -> None:
        """Regression guard: the object handed to send_message is a real
        parsed EmailMessage, not the raw bytes.
        """
        client = mock_smtp.return_value.__enter__.return_value
        sender = SmtpSender("h", 587, "u@example.com", "p")

        sender.send(_raw_with_bcc(), ["alice@example.net"])

        sent = client.send_message.call_args.args[0]
        # Re-serialize round-trips cleanly.
        reparsed = message_from_bytes(sent.as_bytes(), policy=_default_policy)
        assert reparsed["Subject"] == "Hi"
