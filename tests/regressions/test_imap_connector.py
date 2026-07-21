"""Tests for ImapConnector."""

from datetime import datetime
from typing import Any
from unittest.mock import MagicMock, patch

from imapclient.exceptions import IMAPClientError
from imapclient.response_types import Address, Envelope

from apple_mail_fast_mcp.imap_connector import (
    ImapConnector,
)

type FolderListing = list[tuple[tuple[bytes, ...], bytes, str]]


def _fake_envelope(
    *,
    message_id: bytes = b"<msg-1@example.com>",
    subject: bytes = b"Hello",
    sender_name: bytes = b"Alice",
    sender_mailbox: bytes = b"alice",
    sender_host: bytes = b"example.com",
    to: tuple[Address, ...] = (),
    cc: tuple[Address, ...] = (),
    date: datetime | None = None,
) -> Envelope:
    """Build an Envelope with reasonable defaults for envelope-shape tests."""
    date = date or datetime(2026, 4, 22, 10, 0, 0)
    from_addr = Address(sender_name, b"", sender_mailbox, sender_host)
    return Envelope(
        date=date,
        subject=subject,
        from_=(from_addr,),
        sender=(from_addr,),
        reply_to=(from_addr,),
        to=to,
        cc=cc,
        bcc=(),
        in_reply_to=b"",
        message_id=message_id,
    )


def _fake_fetch_result(uids: list[int]) -> dict[int, dict[bytes, Any]]:
    """Build a FETCH-style dict with ENVELOPE + FLAGS for given UIDs."""
    return {
        uid: {
            b"ENVELOPE": _fake_envelope(
                message_id=f"<msg-{uid}@example.com>".encode(),
                subject=f"Subject {uid}".encode(),
            ),
            b"FLAGS": (b"\\Seen",),
        }
        for uid in uids
    }


_BS_PLAIN_TEXT_LEAF = (
    b"text",
    b"plain",
    (b"CHARSET", b"UTF-8"),
    None,
    None,
    b"7bit",
    100,
    5,
    None,
    None,
    None,
    None,
)


class TestLimitWithHasAttachmentFilter:
    """`limit` must bound MATCHING results, not the candidate window.

    The old `uids[-limit:]` pre-truncation made `limit=5,
    has_attachment=True` mean "whichever of the 5 newest messages happen
    to have attachments" — observed live as 1/2/6 results for the same
    mailbox depending on what was in the window, silently missing
    attachment-bearing messages. The AppleScript path collects matches
    until limit; the IMAP path must agree.
    """

    def _setup(self, mock_cls, *, uids, attachment_uids):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = list(uids)
        full = {
            uid: {
                b"ENVELOPE": _fake_envelope(
                    message_id=f"<msg-{uid}@example.com>".encode(),
                    subject=f"Subject {uid}".encode(),
                ),
                b"FLAGS": (b"\\Seen",),
                b"BODYSTRUCTURE": (
                    _BS_REAL_ICLOUD_MIXED_PDF if uid in attachment_uids else _BS_PLAIN_TEXT_LEAF
                ),
            }
            for uid in uids
        }
        mock_client.fetch.side_effect = lambda chunk, keys: {uid: full[uid] for uid in chunk}
        return mock_client

    @patch("apple_mail_fast_mcp.imap_connector.IMAPClient")
    def test_uid_expunged_between_search_and_fetch_is_skipped(self, mock_cls):
        """A UID the server omits from the FETCH response (expunged by
        another session, RFC 3501 / #314) is skipped within the chunked
        walk, not a KeyError aborting the whole search.
        """
        client = self._setup(mock_cls, uids=range(1, 11), attachment_uids={3, 7})
        inner = client.fetch.side_effect
        client.fetch.side_effect = lambda chunk, keys: {
            uid: entry for uid, entry in inner(chunk, keys).items() if uid != 7
        }
        conn = ImapConnector("h", 993, "u@e.com", "pw")
        result = conn.search_messages(has_attachment=True, limit=5)
        assert [r["subject"] for r in result] == ["Subject 3"]


# BODYSTRUCTURE shapes below match what IMAPClient returns: either a flat
# leaf tuple (type, subtype, params, id, desc, encoding, size, [type-specific], [disposition])
# or a multipart tuple ((child1,), (child2,), ..., subtype).
_LEAF_TEXT = (b"text", b"plain", (), None, None, b"7bit", 100, 5)
_LEAF_PDF_ATTACHMENT = (
    b"application",
    b"pdf",
    (b"name", b"x.pdf"),
    None,
    None,
    b"base64",
    2048,
    (b"attachment", (b"filename", b"x.pdf")),
)
_MULTIPART_WITH_ATTACHMENT = (_LEAF_TEXT, _LEAF_PDF_ATTACHMENT, b"mixed")


class TestEnvelopeTranslation:
    @patch("apple_mail_fast_mcp.imap_connector.IMAPClient")
    def test_emits_both_id_and_rfc_message_id_dual_emit(self, mock_cls):
        """#148: every IMAP-path row carries `rfc_message_id` alongside
        `id`. On this path the two are intentionally identical (both
        are the RFC 5322 Message-ID, bracketless).
        """
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.search.return_value = [1]
        mock_client.fetch.return_value = {
            1: {
                b"ENVELOPE": _fake_envelope(message_id=b"<dual@example.com>"),
                b"FLAGS": (),
            }
        }
        [msg] = ImapConnector("h", 993, "u@e.com", "pw").search_messages()
        assert msg["id"] == "dual@example.com"
        assert msg["rfc_message_id"] == "dual@example.com"
        assert msg["id"] == msg["rfc_message_id"]


# ---------------------------------------------------------------------------
# Issue #72: get_message
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Issue #73: get_attachments
# ---------------------------------------------------------------------------

# BODYSTRUCTURE fixtures for the attachment extractor. Leaf shape is
# (type, subtype, params, id, desc, encoding, size, [extras...], [disposition]).
# Disposition tuple is (kind, params) where params is flat (k,v,k,v,...).

# Plain body — should never be reported as an attachment.
_BS_PLAIN_TEXT = (b"text", b"plain", (), None, None, b"7bit", 100, 5)

# PDF attachment — disposition = attachment, filename in disposition params.
_BS_PDF_ATTACHMENT = (
    b"application",
    b"pdf",
    (b"name", b"report.pdf"),
    None,
    None,
    b"base64",
    524288,
    (b"attachment", (b"filename", b"report.pdf")),
)

# JPEG attachment — different mime + size.
_BS_JPEG_ATTACHMENT = (
    b"image",
    b"jpeg",
    (b"name", b"photo.jpg"),
    None,
    None,
    b"base64",
    4096,
    (b"attachment", (b"filename", b"photo.jpg")),
)

# Inline image with filename — multipart/related signature image case.
_BS_INLINE_IMAGE_WITH_FILENAME = (
    b"image",
    b"png",
    (b"name", b"sig.png"),
    b"<sig@local>",
    None,
    b"base64",
    2048,
    (b"inline", (b"filename", b"sig.png")),
)

# Inline body part WITHOUT a filename — a real body, not an attachment.
_BS_INLINE_BODY_NO_FILENAME = (
    b"text",
    b"html",
    (b"charset", b"utf-8"),
    None,
    None,
    b"7bit",
    200,
    10,
    (b"inline", ()),
)

# Forwarded email (message/rfc822). Per RFC 2046 §5.2.1, the leaf for
# message/rfc822 carries an envelope + body + lines after the size field;
# disposition may or may not be present. Without disposition, the
# AppleScript path silently drops these — IMAP must still surface them.
_BS_FORWARDED_EMAIL_NO_DISP = (
    b"message",
    b"rfc822",
    (),
    None,
    None,
    b"7bit",
    8192,
    None,
    None,
    250,  # envelope, body, lines (None'd; we don't inspect them)
)

# Legacy: filename in content-type's `name` param, no disposition at all.
_BS_LEGACY_NAME_PARAM_ONLY = (
    b"application",
    b"zip",
    (b"name", b"old.zip"),
    None,
    None,
    b"base64",
    1024,
)

# Unicode filename via UTF-8 bytes.
_BS_UNICODE_FILENAME = (
    b"application",
    b"pdf",
    (),
    None,
    None,
    b"base64",
    100,
    (b"attachment", (b"filename", b"r\xc3\xa9sum\xc3\xa9.pdf")),
)

# Mangled bytes that aren't valid UTF-8.
_BS_MANGLED_FILENAME = (
    b"application",
    b"pdf",
    (),
    None,
    None,
    b"base64",
    100,
    (b"attachment", (b"filename", b"\xff\xfe\xff.pdf")),
)

# A BODYSTRUCTURE captured verbatim from a real iCloud message (a
# multipart/mixed with a multipart/alternative body + an application/pdf
# attachment). The crucial shape detail: IMAPClient groups multipart
# children in a LIST at position 0 — ([child1, child2], b"mixed", ...) —
# NOT as bare tuple elements. The other multipart fixtures above use the
# bare-tuple shape, which imapclient never actually emits.
_BS_REAL_ICLOUD_MIXED_PDF = (
    [
        (
            [
                (
                    b"text",
                    b"plain",
                    (b"CHARSET", b"UTF-8"),
                    None,
                    None,
                    b"quoted-printable",
                    454,
                    27,
                    None,
                    None,
                    None,
                    None,
                ),
                (
                    b"text",
                    b"html",
                    (b"CHARSET", b"UTF-8"),
                    None,
                    None,
                    b"quoted-printable",
                    1915,
                    33,
                    None,
                    None,
                    None,
                    None,
                ),
            ],
            b"alternative",
            (b"BOUNDARY", b"000000000000578ec60652f6a8ad"),
            None,
            None,
            None,
        ),
        (
            b"application",
            b"pdf",
            (b"NAME", b"04 FS.pdf"),
            b"<f_mpr37zve0>",
            None,
            b"base64",
            289236,
            None,
            (b"ATTACHMENT", (b"FILENAME", b"04 FS.pdf")),
            None,
            None,
        ),
    ],
    b"mixed",
    (b"BOUNDARY", b"000000000000578ec70652f6a8af"),
    None,
    None,
    None,
)


class TestGetAttachments:
    """ImapConnector.get_attachments — Message-ID lookup + BODYSTRUCTURE walk."""

    def _setup_client(
        self,
        mock_cls: MagicMock,
        *,
        uids: list[int] | None = None,
        bodystructure: Any = None,
    ) -> MagicMock:
        client = MagicMock()
        mock_cls.return_value = client
        client.search.return_value = uids if uids is not None else [42]
        if bodystructure is None:
            bodystructure = _BS_PLAIN_TEXT
        client.fetch.return_value = {
            (uids or [42])[0]: {b"BODYSTRUCTURE": bodystructure},
        }
        return client

    @patch("apple_mail_fast_mcp.imap_connector.IMAPClient")
    def test_real_imapclient_multipart_list_shape_enumerates_pdf(self, mock_cls: MagicMock) -> None:
        """Regression: IMAPClient groups multipart children in a list at
        position 0. Walking only the bare-tuple shape (as the walker did
        before) misreads a real multipart/mixed as a leaf and drops the
        attachment. Uses a BODYSTRUCTURE captured verbatim from real iCloud,
        and also checks the sibling has-attachment walker agrees.
        """
        from apple_mail_fast_mcp.imap_connector import _bodystructure_has_attachment

        self._setup_client(mock_cls, bodystructure=_BS_REAL_ICLOUD_MIXED_PDF)

        result = ImapConnector("h", 993, "u@e.com", "pw").get_attachments(
            "abc@x",
            mailbox="INBOX",
        )

        assert len(result) == 1
        assert result[0]["name"] == "04 FS.pdf"
        assert result[0]["mime_type"] == "application/pdf"
        assert result[0]["size"] == 289236
        assert _bodystructure_has_attachment(_BS_REAL_ICLOUD_MIXED_PDF) is True


# ---------------------------------------------------------------------------
# Mailbox write operations (#162, #163): delete_mailbox / rename_mailbox
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Issue #122: Gmail X-GM-THRID dispatch in find_thread_members
# ---------------------------------------------------------------------------


def _gmail_caps_with_xgm() -> set[bytes]:
    """Capability list as Gmail returns it post-login (live-probed
    2026-05-02 — see docs/research/imap-thread-strategies.md addendum).
    """
    return {
        b"IMAP4REV1",
        b"UNSELECT",
        b"IDLE",
        b"NAMESPACE",
        b"QUOTA",
        b"ID",
        b"XLIST",
        b"CHILDREN",
        b"X-GM-EXT-1",
        b"UIDPLUS",
        b"COMPRESS=DEFLATE",
        b"ENABLE",
        b"MOVE",
        b"CONDSTORE",
        b"ESEARCH",
        b"UTF8=ACCEPT",
        b"LIST-EXTENDED",
        b"LIST-STATUS",
        b"LITERAL-",
        b"SPECIAL-USE",
        b"APPENDLIMIT=35651584",
    }


def _generic_caps_no_xgm() -> set[bytes]:
    """Capability list for a non-Gmail server (e.g. iCloud, Fastmail).
    No X-GM-EXT-1 → Tier 1 must skip.
    """
    return {
        b"IMAP4REV1",
        b"UNSELECT",
        b"IDLE",
        b"NAMESPACE",
        b"QUOTA",
        b"ID",
        b"UIDPLUS",
        b"ENABLE",
        b"CONDSTORE",
        b"ESEARCH",
        b"SPECIAL-USE",
    }


def _gmail_folder_listing_with_all() -> FolderListing:
    """A Gmail-style folder listing with the \\All SPECIAL-USE flag."""
    return [
        ((b"\\HasNoChildren",), b"/", "INBOX"),
        ((b"\\HasNoChildren", b"\\Drafts"), b"/", "[Gmail]/Drafts"),
        ((b"\\HasNoChildren", b"\\Sent"), b"/", "[Gmail]/Sent Mail"),
        ((b"\\HasNoChildren", b"\\All"), b"/", "[Gmail]/All Mail"),
        ((b"\\HasNoChildren", b"\\Trash"), b"/", "[Gmail]/Trash"),
        ((b"\\HasNoChildren", b"\\Junk"), b"/", "[Gmail]/Spam"),
    ]


def _localized_gmail_folder_listing() -> FolderListing:
    """A localized Gmail (Italian) listing — \\All flag present, name
    differs. Hardcoding `[Gmail]/All Mail` would miss this; SPECIAL-USE
    is the robust answer.
    """
    return [
        ((b"\\HasNoChildren",), b"/", "INBOX"),
        ((b"\\HasNoChildren", b"\\All"), b"/", "[Google Mail]/Tutta la posta"),
    ]


def _generic_folder_listing_no_all() -> FolderListing:
    """A non-Gmail listing with no \\All flag — Tier 1 must skip even
    when the capability is advertised.
    """
    return [
        ((b"\\HasNoChildren",), b"/", "INBOX"),
        ((b"\\HasNoChildren", b"\\Sent"), b"/", "Sent"),
        ((b"\\HasNoChildren", b"\\Trash"), b"/", "Trash"),
    ]


class TestImapDeleteMessages:
    """Issue #150: server-side delete via UID MOVE to the account's
    Trash folder (RFC 6851), or UID COPY + STORE \\Deleted + UID
    EXPUNGE (RFC 4315 UIDPLUS). Trash folder is resolved via RFC 6154
    SPECIAL-USE \\Trash flag with conventional-name fallback.
    """

    @staticmethod
    def _trash_listing(special_use: bool = True, trash_name: str = "Trash") -> FolderListing:
        """A folder listing with (or without) a SPECIAL-USE \\Trash."""
        if special_use:
            return [
                ((b"\\HasNoChildren",), b"/", "INBOX"),
                ((b"\\HasNoChildren", b"\\Trash"), b"/", trash_name),
            ]
        return [
            ((b"\\HasNoChildren",), b"/", "INBOX"),
            ((b"\\HasNoChildren",), b"/", trash_name),
        ]

    @patch("apple_mail_fast_mcp.imap_connector.IMAPClient")
    def test_delete_resolves_trash_before_selecting_source(self, mock_cls: MagicMock) -> None:
        """All LIST traffic must run before SELECT. Some servers
        (Exchange Online, older Dovecot) implicitly CLOSE the selected
        mailbox when LIST runs while SELECTED, which would make the
        subsequent SEARCH fail with "No mailbox selected" and silently
        kick the operation onto the slower AppleScript fallback. (#199)
        """
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = {b"MOVE"}
        client.list_folders.return_value = self._trash_listing()
        client.search.return_value = [1]

        ImapConnector("h", 993, "u@e.com", "pw").delete_messages(
            ["a@x"],
            source_mailbox="INBOX",
        )

        call_names = [c[0] for c in client.method_calls]
        list_idx = call_names.index("list_folders")
        select_idx = call_names.index("select_folder")
        assert list_idx < select_idx, (
            f"list_folders (call #{list_idx}) must run before "
            f"select_folder (call #{select_idx}) — see #199"
        )


class TestImapSetReadStatus:
    """Issue #151: server-side read/unread via UID STORE +/-FLAGS
    (\\Seen). \\Seen is base IMAP (RFC 3501), universal across servers
    — no capability check needed, no fallback variants.
    """

    @patch("apple_mail_fast_mcp.imap_connector.IMAPClient")
    def test_set_read_status_no_capability_check_required(self, mock_cls: MagicMock) -> None:
        """\\Seen is RFC 3501 base IMAP — universal. Don't gate behind
        a capability check (regression guard against accidental
        cap-gating).
        """
        client = MagicMock()
        mock_cls.return_value = client
        client.search.return_value = [1]

        ImapConnector("h", 993, "u@e.com", "pw").set_read_status(
            ["a@x"],
            source_mailbox="INBOX",
            read=True,
        )
        client.capabilities.assert_not_called()


class TestImapSetFlaggedStatus:
    """Issue #152: server-side flag/unflag via UID STORE +/-FLAGS
    (\\Flagged). Like \\Seen in #151, \\Flagged is base IMAP (RFC 3501) —
    universal across servers, no capability check needed.

    Mail.app's flag-color attributes (the $MailFlagBit* user keywords)
    are Mail.app-specific and out of scope for IMAP. This IMAP path
    only handles the no-color case; flag_color goes via AppleScript.
    """

    @patch("apple_mail_fast_mcp.imap_connector.IMAPClient")
    def test_set_flagged_status_no_capability_check_required(self, mock_cls: MagicMock) -> None:
        """\\Flagged is RFC 3501 base IMAP — universal. Don't gate
        behind a capability check (regression guard).
        """
        client = MagicMock()
        mock_cls.return_value = client
        client.search.return_value = [1]

        ImapConnector("h", 993, "u@e.com", "pw").set_flagged_status(
            ["a@x"],
            source_mailbox="INBOX",
            flagged=True,
        )
        client.capabilities.assert_not_called()


# ---------------------------------------------------------------------------
# Issue #125: Gmail X-GM-THRID per-mailbox iteration (Tier 1.5)
# ---------------------------------------------------------------------------


def _gmail_folder_listing_no_all_with_sent() -> FolderListing:
    """Gmail-style listing where [Gmail]/All Mail is hidden (per-folder
    IMAP opt-out). \\Sent is still visible. This is the configuration
    Tier 1.5 (#125) is designed for.
    """
    return [
        ((b"\\HasNoChildren",), b"/", "INBOX"),
        ((b"\\HasNoChildren", b"\\Drafts"), b"/", "[Gmail]/Drafts"),
        ((b"\\HasNoChildren", b"\\Sent"), b"/", "[Gmail]/Sent Mail"),
        ((b"\\HasNoChildren", b"\\Trash"), b"/", "[Gmail]/Trash"),
        ((b"\\HasNoChildren",), b"/", "Receipts"),
        ((b"\\HasNoChildren",), b"/", "Newsletters"),
    ]


# ---------------------------------------------------------------------------
# Issue #123: RFC 5256 THREAD dispatch (Tier 2)
# ---------------------------------------------------------------------------


def _fastmail_caps_with_thread() -> set[bytes]:
    """Capability list for a Fastmail-like server: THREAD=REFERENCES
    advertised, no X-GM-EXT-1.
    """
    return {
        b"IMAP4REV1",
        b"UNSELECT",
        b"IDLE",
        b"NAMESPACE",
        b"QUOTA",
        b"ID",
        b"UIDPLUS",
        b"ENABLE",
        b"CONDSTORE",
        b"ESEARCH",
        b"SPECIAL-USE",
        b"THREAD=REFERENCES",
    }


def _caps_with_thread_refs_alias() -> set[bytes]:
    """Capability set advertising the THREAD=REFS alias (RFC 5256)."""
    return {
        b"IMAP4REV1",
        b"UNSELECT",
        b"IDLE",
        b"NAMESPACE",
        b"UIDPLUS",
        b"ENABLE",
        b"THREAD=REFS",
    }


def _fastmail_folder_listing() -> FolderListing:
    """A small folder listing for THREAD-dispatch tests."""
    return [
        ((b"\\HasNoChildren",), b"/", "INBOX"),
        ((b"\\HasNoChildren", b"\\Sent"), b"/", "Sent"),
        ((b"\\HasNoChildren",), b"/", "Archive"),
    ]


class TestFindThreadMembersImapThread:
    """Tier 2 (RFC 5256 THREAD, #123) dispatch."""

    @patch("apple_mail_fast_mcp.imap_connector.IMAPClient")
    def test_thread_command_rejection_falls_through_to_bfs(self, mock_cls: MagicMock) -> None:
        """If client.thread() raises mid-flight (server lied about
        THREAD capability), Tier 2 returns None → BFS runs.

        #172: this abort-and-fall-through behavior is intentional
        even though the other per-mailbox error paths (SELECT /
        search / fetch) just ``continue``. THREAD failure casts doubt
        on earlier mailboxes' THREAD output in a way that local
        search/fetch failures don't. See the inline comment in
        ``_thread_via_imap_thread`` for the asymmetry rationale.
        """
        client = MagicMock()
        mock_cls.return_value = client
        client.capabilities.return_value = _fastmail_caps_with_thread()
        client.list_folders.return_value = _fastmail_folder_listing()

        client.search.side_effect = [
            [1],  # INBOX MsgID
            [],  # INBOX References
        ] + [[]] * 100  # plenty for BFS to fast-exit
        client.thread.side_effect = IMAPClientError("THREAD rejected")
        client.fetch.return_value = {}

        ImapConnector("h", 993, "u@e.com", "pw").find_thread_members(
            anchor_rfc_message_id="anchor@example.com",
            anchor_references=[],
        )

        # Tier 2 only made 2 SEARCHes (first folder, until THREAD raised).
        # BFS then ran more SEARCHes — assert the count exceeds Tier 2's.
        assert client.search.call_count > 2
