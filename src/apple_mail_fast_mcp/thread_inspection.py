"""Bounded, explicitly scoped thread evidence after the September draft incident."""

from __future__ import annotations

import copy
from datetime import UTC
from email import policy
from email.parser import HeaderParser
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any

from .draft_inspection import _scoped_message_clause
from .utils import (
    applescript_account_clause,
    escape_applescript_string,
    normalize_subject,
    parse_applescript_json,
    parse_rfc822_ids,
    sanitize_input,
    walk_thread_graph,
)

if TYPE_CHECKING:
    from .mail_connector import AppleMailConnector


def _row_script(message_variable: str, mailbox_variable: str) -> str:
    return f"""{{|id|:(id of {message_variable} as text), |rfc_message_id|:(message id of {message_variable}), |subject|:(subject of {message_variable}), |sender|:(sender of {message_variable}), |date_received|:(date received of {message_variable} as text), |read_status|:(read status of {message_variable}), |flagged|:(flagged status of {message_variable}), |mailbox|:{mailbox_variable}, |headers_raw|:(all headers of {message_variable})}}"""


def _enrich_row(row: dict[str, Any], account: str) -> dict[str, Any]:
    headers = HeaderParser(policy=policy.default).parsestr(str(row.pop("headers_raw", "")))
    reply_ids = parse_rfc822_ids(str(headers.get("In-Reply-To", "")))
    references = parse_rfc822_ids(str(headers.get("References", "")))
    row.update(
        {
            "account": account,
            "in_reply_to": reply_ids[0] if reply_ids else "",
            "references_parsed": references,
            "references": references,
            "to": str(headers.get("To", "")),
            "cc": str(headers.get("Cc", "")),
            "date_header": str(headers.get("Date", "")),
        }
    )
    row["rfc_message_id"] = str(row.get("rfc_message_id") or headers.get("Message-ID", "")).strip(
        "<>"
    )
    try:
        received = parsedate_to_datetime(row["date_header"])
        row["date_sort"] = received.astimezone(UTC).isoformat() if received.tzinfo else ""
    except ValueError, TypeError, OverflowError:
        row["date_sort"] = ""
    return row


def get_scoped_thread(
    connector: AppleMailConnector,
    message_id: str,
    *,
    account: str,
    mailbox: str,
    search_mailboxes: list[str] | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Return RFC-connected members from exact folders with truthful completeness.

    Reads metadata and RFC headers, never message bodies. The default search
    covers only the anchor mailbox; supply the exact Sent/Drafts/archive paths
    to include those locations. No cross-account or account-wide scan occurs.
    """
    from .mail_connector import (  # ruff: ignore[import-outside-top-level] - avoid a connector import cycle
        _MAILBOX_RESOLVER_HANDLERS,
        _wrap_as_json_script,
    )

    if not account or not mailbox:
        raise ValueError("An exact account and anchor mailbox are required")
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    folders = list(dict.fromkeys([mailbox, *(search_mailboxes or [])]))
    if len(folders) > 8 or any(not folder for folder in folders):
        raise ValueError("Provide at most eight nonempty exact mailbox paths")
    bounded = copy.copy(connector)
    bounded.timeout = 12
    account_clause = applescript_account_clause(account)
    mailbox_safe = escape_applescript_string(sanitize_input(mailbox))
    anchor_script = _wrap_as_json_script(
        f'''
        tell application "Mail"
            set acc to {account_clause}
            set anchorMailbox to "{mailbox_safe}"
            set mb to my resolveMailbox(acc, anchorMailbox)
            set msg to first message of mb whose ({_scoped_message_clause(message_id)})
            set resultData to {_row_script("msg", "anchorMailbox")}
        end tell
        ''',
        timeout=8,
        handlers=_MAILBOX_RESOLVER_HANDLERS,
    )
    raw = parse_applescript_json(bounded._run_applescript(anchor_script))  # ruff: ignore[private-member-access] - mandated connector boundary
    if not isinstance(raw, dict):
        raise TypeError("Thread anchor read did not return a JSON object")
    anchor = _enrich_row(raw, account)
    scope = {
        "account": account,
        "anchor_mailbox": mailbox,
        "search_mailboxes": folders,
        "candidate_limit": limit,
    }
    base_subject = normalize_subject(str(anchor.get("subject", "")))
    if not base_subject:
        return {
            "success": True,
            "thread": [anchor],
            "count": 1,
            "scope": scope,
            "complete": False,
            "scope_complete": False,
            "warnings": [
                "Anchor has an empty base subject; no unbounded candidate scan was attempted"
            ],
        }
    folder_list = ", ".join(
        f'"{escape_applescript_string(sanitize_input(folder))}"' for folder in folders
    )
    subject_safe = escape_applescript_string(sanitize_input(base_subject))
    candidate_script = _wrap_as_json_script(
        f'''
        tell application "Mail"
            set acc to {account_clause}
            set candidateRows to {{}}
            set readErrors to {{}}
            set finishedMailboxes to {{}}
            set hitLimit to false
            set deadlineReached to false
            set inspectionStarted to current date
            repeat with folderName in {{{folder_list}}}
                if ((current date) - inspectionStarted) is greater than 18 then
                    set deadlineReached to true
                    exit repeat
                end if
                try
                    set mb to my resolveMailbox(acc, folderName as text)
                    set hits to messages of mb whose subject contains "{subject_safe}"
                    repeat with msg in hits
                        if ((current date) - inspectionStarted) is greater than 18 then
                            set deadlineReached to true
                            exit repeat
                        end if
                        if (count of candidateRows) is greater than or equal to {limit} then
                            set hitLimit to true
                            exit repeat
                        end if
                        try
                            set end of candidateRows to {_row_script("msg", "(folderName as text)")}
                        on error errText number errNumber
                            set end of readErrors to {{|mailbox|:(folderName as text), |error|:errText, |code|:errNumber}}
                        end try
                    end repeat
                    if not hitLimit and not deadlineReached then set end of finishedMailboxes to (folderName as text)
                on error errText number errNumber
                    set end of readErrors to {{|mailbox|:(folderName as text), |error|:errText, |code|:errNumber}}
                end try
                if hitLimit or deadlineReached then exit repeat
            end repeat
            set resultData to {{|candidates|:candidateRows, |errors|:readErrors, |finished_mailboxes|:finishedMailboxes, |hit_limit|:hitLimit, |deadline_reached|:deadlineReached}}
        end tell
        ''',
        timeout=4,
        handlers=_MAILBOX_RESOLVER_HANDLERS,
    )
    bounded.timeout = 26
    try:
        candidate_data = parse_applescript_json(bounded._run_applescript(candidate_script))  # ruff: ignore[private-member-access] - mandated connector boundary
    except Exception as exc:
        return {
            "success": True,
            "thread": [anchor],
            "count": 1,
            "scope": scope,
            "complete": False,
            "scope_complete": False,
            "errors": [{"stage": "candidate_read", "error": str(exc)}],
            "warnings": ["Only the anchor was retrieved; candidate lookup failed or timed out"],
        }
    if not isinstance(candidate_data, dict):
        raise TypeError("Thread candidate read did not return a JSON object")
    candidates = [
        _enrich_row(row, account)
        for row in candidate_data.get("candidates", [])
        if isinstance(row, dict)
    ]
    known_ids = {
        str(anchor["rfc_message_id"]),
        str(anchor["in_reply_to"]),
        *anchor["references"],
    } - {""}
    accepted = walk_thread_graph(
        known_ids, [row for row in candidates if row["id"] != anchor["id"]]
    )
    thread = [anchor, *accepted]
    thread.sort(key=lambda row: str(row.get("date_sort", "")))
    for row in thread:
        row.pop("date_sort", None)
        row.pop("references_parsed", None)
    errors = candidate_data.get("errors", [])
    scope_complete = (
        not errors
        and not candidate_data.get("hit_limit")
        and not candidate_data.get("deadline_reached")
        and set(candidate_data.get("finished_mailboxes", [])) == set(folders)
    )
    return {
        "success": True,
        "thread": thread,
        "count": len(thread),
        "scope": scope,
        "complete": False,
        "scope_complete": scope_complete,
        "candidate_count": len(candidates),
        "finished_mailboxes": candidate_data.get("finished_mailboxes", []),
        "candidate_limit_reached": bool(candidate_data.get("hit_limit")),
        "deadline_reached": bool(candidate_data.get("deadline_reached")),
        "errors": errors,
        "warnings": [
            "Subject-prefiltered RFC header reconstruction; changed-subject members and messages outside the requested folders may be absent",
            "Mail conversation UI and previous-message bodies were not inspected",
        ],
    }
