"""Unit tests for the clean RFC822 draft builder (issue #245).

The builder exists so drafts can be created via IMAP APPEND instead of
Mail.app's AppleScript ``content`` setter, which wraps every body in an
``Apple-Mail-URLShareWrapper`` ``<blockquote type="cite">`` (renders as a
quote on iOS). These tests pin the clean output.
"""

from __future__ import annotations

# --- HTML body support (issue #251) --------------------------------------
# --- Reply/forward extensions (issue #245 follow-up) ---------------------
from apple_mail_fast_mcp.draft_builder import (
    build_draft_mime,
)

# --- byte-fetch attachment enumeration (IMAP index-contract fix) ---------
#
# get_attachment_content / save_attachments (IMAP) index into this list; it
# MUST agree, in count and order, with the BODYSTRUCTURE metadata walk that
# get_attachments / get_messages report (imap_connector
# ._bodystructure_extract_attachments). email.iter_attachments() does NOT —
# it drops body-referenced inline parts and skips parts nested under a
# multipart/alternative — which broke the shared 0-based index contract
# (out-of-range, or the WRONG part's bytes). Cases below mirror the real
# iCloud divergences found by dogfooding.


def _names_types(atts):
    return [(name, f"{mt}/{st}") for (name, mt, st, _b) in atts]


# --- Message-ID domain: no reverse-DNS, sender-derived (#408) --------------


def test_message_id_domain_matches_sender_and_needs_no_dns():
    """#408: the Message-ID domain is taken from the sender's address, so
    make_msgid() never falls back to socket.getfqdn() (a reverse-DNS lookup
    that stalls ~5s per call in CI). The autouse conftest guard would raise
    if any real DNS were attempted here, so a green result also proves no
    getfqdn call.
    """
    msgid, _raw = build_draft_mime(
        sender="email@fmasi.eu",
        to=["someone@example.com"],
        subject="Hi",
        body="body",
    )
    assert msgid.endswith("@fmasi.eu>")
