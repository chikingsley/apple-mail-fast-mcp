"""Saved-message evidence for the September 2026 incorrectly threaded draft incident.

Reading Mail's plain ``content`` cannot establish reply linkage, HTML quote
placement, signature preservation, or fonts. This module reads the exact saved
message and exposes MIME evidence without pretending to inspect rendered Mail UI.
"""

from __future__ import annotations

import copy
import re
from email import policy
from email.parser import Parser
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any, override

from .utils import (
    applescript_account_clause,
    escape_applescript_string,
    parse_applescript_json,
    parse_rfc822_ids,
    sanitize_input,
)

if TYPE_CHECKING:
    from email.message import MIMEPart

    from .mail_connector import AppleMailConnector

_MAX_SOURCE_CHARS = 16 * 1024 * 1024
_MAX_PARTS = 100
_MAX_DEPTH = 20
_MAX_HTML_CHARS = 256 * 1024
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_FONT_DECLARATION = re.compile(
    r"(?:^|;)\s*(font(?:-family|-size|-weight|-style)?)\s*:\s*([^;]+)", re.IGNORECASE
)


def _normalized(value: str) -> str:
    return " ".join(value.split())


def _scoped_message_clause(message_id: str) -> str:
    """Numeric metadata IDs must not also fetch RFC IDs across an EWS folder."""
    from .mail_connector import (  # ruff: ignore[import-outside-top-level] - avoid connector cycle
        _message_id_match_clause,
    )

    if re.fullmatch(r"[0-9]+", message_id):
        return f"id is {int(message_id)}"
    return _message_id_match_clause(message_id)


def _bounded_text(value: str, max_chars: int) -> dict[str, Any]:
    return {
        "text": value[:max_chars],
        "characters": len(value),
        "truncated": len(value) > max_chars,
    }


class _StructureParser(HTMLParser):
    """Collect structural evidence; this deliberately does not compute CSS layout."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, str]] = []
        self.text_by_region: dict[str, list[str]] = {"authored": [], "quoted": [], "signature": []}
        self.fonts: list[dict[str, str]] = []
        self.quote_count = 0
        self.signature_count = 0
        self.style_blocks: list[str] = []
        self.outlook_quote_tail = False

    def _region(self) -> str:
        regions = [region for _, region in self.stack]
        if "ignored" in regions:
            return "ignored"
        if "quoted" in regions or self.outlook_quote_tail:
            return "quoted"
        if "signature" in regions:
            return "signature"
        return "authored"

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        identifier = (values.get("id", "") + " " + values.get("class", "")).lower()
        # Outlook's reply header and old body are siblings, not a blockquote.
        # The exact reply boundary marks the remainder of that message as history.
        if values.get("id", "").lower() == "divrplyfwdmsg":
            self.outlook_quote_tail = True
            self.quote_count += 1
        region = self._region()
        if tag in {"script", "style", "head"}:
            region = "ignored"
        elif tag == "blockquote" or any(
            marker in identifier.split() for marker in ("gmail_quote", "yahoo_quoted")
        ):
            region = "quoted"
            self.quote_count += 1
        elif (
            "applemailsignature" in identifier.split()
            or values.get("id", "").lower() == "signature"
        ):
            region = "signature"
            if self._region() != "quoted":
                self.signature_count += 1
        if tag not in _VOID_TAGS:
            self.stack.append((tag, region))
        if tag in {"div", "p", "br", "li", "tr"} and region in self.text_by_region:
            self.text_by_region[region].append("\n")
        if len(self.fonts) < _MAX_PARTS:
            for match in _FONT_DECLARATION.finditer(values.get("style", "")):
                self.fonts.append(
                    {
                        "region": self._region(),
                        "tag": tag,
                        "property": match[1].lower(),
                        "value": match[2].strip()[:500],
                    }
                )
            if tag == "font" and values.get("face"):
                self.fonts.append(
                    {
                        "region": self._region(),
                        "tag": tag,
                        "property": "font-family",
                        "value": values["face"][:500],
                    }
                )

    @override
    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    @override
    def handle_endtag(self, tag: str) -> None:
        for position in range(len(self.stack) - 1, -1, -1):
            if self.stack[position][0] == tag:
                del self.stack[position:]
                break

    @override
    def handle_data(self, data: str) -> None:
        if any(tag == "style" for tag, _ in self.stack):
            self.style_blocks.append(data)
        region = self._region()
        if region in self.text_by_region:
            self.text_by_region[region].append(data)


def _mime_parts(message: MIMEPart) -> tuple[list[dict[str, Any]], dict[str, list[str]], bool]:
    parts: list[dict[str, Any]] = []
    bodies: dict[str, list[str]] = {"text/plain": [], "text/html": []}
    pending = [(message, 0, "0")]
    complete = True
    while pending:
        part, depth, path = pending.pop()
        if len(parts) >= _MAX_PARTS or depth > _MAX_DEPTH:
            complete = False
            continue
        content_type = part.get_content_type()
        disposition = part.get_content_disposition()
        parts.append(
            {
                "path": path,
                "content_type": content_type,
                "disposition": disposition,
                "filename": part.get_filename(),
                "charset": part.get_content_charset(),
            }
        )
        # Embedded RFC messages and attachment bodies are not the current reply.
        if disposition == "attachment" or content_type == "message/rfc822":
            continue
        if part.is_multipart():
            children = list(part.iter_parts())
            pending.extend(
                (child, depth + 1, f"{path}.{index}")
                for index, child in reversed(list(enumerate(children)))
            )
        elif content_type in bodies:
            try:
                content = part.get_content()
                if isinstance(content, str):
                    bodies[content_type].append(content)
            except LookupError, UnicodeError, ValueError:
                complete = False
    return parts, bodies, complete


def _check_text(expected: str | None, available: str | None, explanation: str) -> dict[str, Any]:
    if not expected:
        return {"status": "not_requested", "reason": "No expected value supplied"}
    if available is None:
        return {"status": "unavailable", "reason": explanation}
    return {
        "status": "passed" if _normalized(expected) in _normalized(available) else "failed",
        "reason": explanation,
    }


def inspect_message_source(
    source: str,
    *,
    max_chars: int = 16000,
    expected_parent_rfc_id: str | None = None,
    expected_body: str | None = None,
    expected_signature: str | None = None,
) -> dict[str, Any]:
    """Parse bounded MIME evidence and evaluate only explicitly supplied expectations."""
    if not 100 <= max_chars <= 100000:
        raise ValueError("max_chars must be between 100 and 100000")
    if not source.strip():
        return {
            "success": False,
            "error_type": "source_unavailable",
            "error": "Saved MIME source is empty; content, thread and formatting are unverified",
        }
    if len(source) > _MAX_SOURCE_CHARS:
        return {
            "success": False,
            "error_type": "source_too_large",
            "source_characters": len(source),
            "error": "MIME source exceeds inspection limit; no verification was performed",
        }
    message = Parser(policy=policy.default).parsestr(source)
    parts, bodies, mime_complete = _mime_parts(message)
    plain = "\n".join(bodies["text/plain"])
    html = "\n".join(bodies["text/html"])
    html_complete = len(html) <= _MAX_HTML_CHARS and mime_complete
    parser = _StructureParser()
    parser.feed(html[:_MAX_HTML_CHARS])
    parser.close()
    regions = {name: "".join(chunks) for name, chunks in parser.text_by_region.items()}
    headers = {
        name: str(message.get(name, ""))
        for name in (
            "Message-ID",
            "In-Reply-To",
            "References",
            "Subject",
            "From",
            "To",
            "Cc",
            "Bcc",
            "Reply-To",
            "Date",
        )
    }
    reply_ids = parse_rfc822_ids(headers["In-Reply-To"])
    references = parse_rfc822_ids(headers["References"])
    parent = (expected_parent_rfc_id or "").strip().strip("<>")
    parent_check: dict[str, Any] = {
        "status": "not_requested",
        "reason": "No expected parent RFC Message-ID supplied",
    }
    if parent:
        parent_check = {
            "status": "passed" if parent in reply_ids else "failed",
            "expected_parent_rfc_id": parent,
            "in_reply_to_matches": parent in reply_ids,
            "references_contains_parent": parent in references,
            "reason": "RFC header linkage only; Mail conversation UI was not inspected",
        }
    region_available = bool(html) and html_complete
    body_check = _check_text(
        expected_body,
        regions["authored"] if region_available else None,
        "Expected reply text must be outside HTML quotation and signature regions; plain text cannot establish this",
    )
    if expected_body and region_available:
        body_check["present_in_quoted_region"] = _normalized(expected_body) in _normalized(
            regions["quoted"]
        )
    signature_check = _check_text(
        expected_signature,
        regions["signature"] if region_available else None,
        "Expected signature text must occur in a nonquoted AppleMailSignature or Outlook Signature region; unmarked signatures require manual inspection",
    )
    statuses = [check["status"] for check in (parent_check, body_check, signature_check)]
    return {
        "success": True,
        "evidence_source": "saved_message_mime",
        "untrusted_content": True,
        "headers": headers,
        "threading": {
            "rfc_message_id": headers["Message-ID"].strip("<>"),
            "in_reply_to": reply_ids,
            "references": references,
            "reply_headers_present": bool(reply_ids),
            "conversation_ui_verified": False,
        },
        "mime": {
            "parts": parts,
            "complete": mime_complete,
            "parse_defects": [type(defect).__name__ for defect in message.defects],
        },
        "body_text": _bounded_text(plain, max_chars),
        "body_html": _bounded_text(html, max_chars),
        "structure": {
            "available": bool(html),
            "complete": bool(html) and html_complete,
            "authored_text": _bounded_text(regions["authored"], max_chars),
            "quoted_text": _bounded_text(regions["quoted"], max_chars),
            "signature_text": _bounded_text(regions["signature"], max_chars),
            "quote_regions": parser.quote_count,
            "signature_regions": parser.signature_count,
            "inline_fonts": parser.fonts[:_MAX_PARTS],
            "style_blocks": _bounded_text("\n".join(parser.style_blocks), max_chars),
        },
        "verification": {
            "status": "failed"
            if "failed" in statuses
            else "incomplete"
            if any(status in {"not_requested", "unavailable"} for status in statuses)
            else "checks_passed",
            "checks": {
                "reply_linkage": parent_check,
                "authored_body": body_check,
                "signature": signature_check,
            },
            "rendered_ui_verified": False,
            "matches_mail_font_preferences": None,
            "limitations": [
                "MIME evidence does not establish rendered appearance or Mail conversation grouping",
                "Inline styles and structural signature markers are evidence, not proof of current account preferences",
                "Quoted earlier messages are embedded content, not separately retrieved thread members",
            ],
        },
    }


def read_saved_message_source(
    connector: AppleMailConnector,
    message_id: str,
    *,
    account: str,
    mailbox: str,
    timeout_seconds: int = 12,
) -> dict[str, Any]:
    """Read one exact saved message; never scan across accounts or mailboxes."""
    from .mail_connector import (  # ruff: ignore[import-outside-top-level] - avoid a connector import cycle
        _MAILBOX_RESOLVER_HANDLERS,
        _wrap_as_json_script,
    )

    if not account or not mailbox:
        raise ValueError("An exact account and mailbox are required")
    if not 1 <= timeout_seconds <= 30:
        raise ValueError("timeout_seconds must be between 1 and 30")
    account_clause = applescript_account_clause(account)
    mailbox_safe = escape_applescript_string(sanitize_input(mailbox))
    clause = _scoped_message_clause(message_id)
    script = _wrap_as_json_script(
        f'''
        tell application "Mail"
            set acc to {account_clause}
            set mb to my resolveMailbox(acc, "{mailbox_safe}")
            set msg to first message of mb whose ({clause})
            set rawSource to source of msg
            if rawSource is missing value then set rawSource to ""
            set sourceCount to count characters of rawSource
            set sourceComplete to (sourceCount is less than or equal to {_MAX_SOURCE_CHARS})
            if not sourceComplete then set rawSource to ""
            set resultData to {{|id|:(id of msg as text), |source|:rawSource, |source_characters|:sourceCount, |source_complete|:sourceComplete}}
        end tell
        ''',
        timeout=timeout_seconds,
        handlers=_MAILBOX_RESOLVER_HANDLERS,
    )
    # A per-call copy limits socket waiting too, without mutating shared timeout.
    bounded = copy.copy(connector)
    bounded.timeout = timeout_seconds + 2
    data = parse_applescript_json(bounded._run_applescript(script))  # ruff: ignore[private-member-access] - mandated connector execution boundary
    if not isinstance(data, dict):
        raise TypeError("Saved-source read did not return a JSON object")
    return data


def inspect_saved_message(
    connector: AppleMailConnector,
    message_id: str,
    *,
    account: str,
    mailbox: str,
    max_chars: int = 16000,
    expected_parent_rfc_id: str | None = None,
    expected_body: str | None = None,
    expected_signature: str | None = None,
) -> dict[str, Any]:
    """Inspect a saved draft or sent message in its exact location."""
    raw = read_saved_message_source(connector, message_id, account=account, mailbox=mailbox)
    context = {"id": str(raw.get("id", message_id)), "account": account, "mailbox": mailbox}
    if not raw.get("source_complete", False):
        return {
            "success": False,
            **context,
            "error_type": "source_incomplete",
            "error": "MIME source unavailable or exceeds limit; draft verification was not performed",
            "source_characters": raw.get("source_characters"),
        }
    return {
        **inspect_message_source(
            str(raw.get("source") or ""),
            max_chars=max_chars,
            expected_parent_rfc_id=expected_parent_rfc_id,
            expected_body=expected_body,
            expected_signature=expected_signature,
        ),
        **context,
    }
