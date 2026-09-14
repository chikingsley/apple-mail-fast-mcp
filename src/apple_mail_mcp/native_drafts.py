"""Native Mail composition preserving quote text and account composition defaults.

September 10 regression: assigning AppleScript ``content`` replaced the original
conversation and signature and introduced an iOS quote wrapper. This path uses
Mail's own reply editor, inserts only the new text, and never silently downgrades.
"""

from __future__ import annotations

import json
import threading
from time import monotonic
from typing import TYPE_CHECKING, Any

from .draft_inspection import inspect_message_source
from .utils import applescript_account_clause, escape_applescript_string, sanitize_input

if TYPE_CHECKING:
    from collections.abc import Callable

_COMPOSE_LOCK = threading.Lock()


class NativeDraftError(RuntimeError):
    """A native compose failure, with any already-created composer identified."""

    def __init__(self, message: str, *, composer_id: str = "", draft_id: str = "") -> None:
        super().__init__(message)
        self.composer_id = composer_id
        self.draft_id = draft_id


def _quoted(value: str) -> str:
    return '"' + escape_applescript_string(sanitize_input(value)) + '"'


def _json_script(body: str, *, handlers: str = "") -> str:
    return (
        'use framework "Foundation"\nuse scripting additions\n' + handlers + "\n"
        "with timeout of 12 seconds\n" + body + "\nend timeout\n"
        "set encoded to current application's NSJSONSerialization's "
        "dataWithJSONObject:resultData options:0 |error|:(missing value)\n"
        "return (current application's NSString's alloc()'s "
        "initWithData:encoded encoding:4) as text"
    )


def _recipient_script(kind: str, addresses: list[str] | None) -> str:
    if addresses is None:
        return ""
    values = ", ".join(_quoted(address) for address in addresses)
    return f"""
    delete every {kind} recipient of d
    repeat with addressValue in {{{values}}}
        make new {kind} recipient at end of {kind} recipients of d with properties {{address:addressValue}}
    end repeat
    """


def creation_script(
    *,
    account: str,
    sender: str,
    seed: str,
    seed_id: str | None,
    seed_mailbox: str | None,
    to: list[str] | None,
    cc: list[str] | None,
    bcc: list[str] | None,
    subject: str | None,
    reply_all: bool,
    attachment_block: str,
    mailbox_handlers: str,
) -> str:
    if seed == "new":
        creation = f"set d to make new outgoing message with properties {{subject:{_quoted(subject or '')}, sender:{_quoted(sender)}, visible:true}}"
    else:
        if not seed_mailbox or not seed_id:
            raise NativeDraftError(
                "Native replies and forwards require the exact seed_mailbox and seed id."
            )
        field = "id" if seed_id.isdecimal() else "message id"
        identifier = seed_id if seed_id.isdecimal() else _quoted(seed_id.strip("<>"))
        verb = "reply"
        if seed == "forward":
            verb = "forward"
        creation = f"""
        set seedBox to my resolveMailbox(targetAccount, {_quoted(seed_mailbox)})
        set originalMessage to first message of seedBox whose {field} is {identifier}
        set parentMessageId to message id of originalMessage
        set d to {verb} originalMessage opening window true {"reply to all true" if seed == "reply" and reply_all else ""}
        """
    subject_override = ""
    if seed != "new" and subject is not None:
        subject_override = f"set subject of d to {_quoted(subject)}"
    return _json_script(
        f"""tell application "Mail"
        set targetAccount to {applescript_account_clause(account)}
        set beforeIds to {{}}
        repeat with previousDraft in messages of drafts mailbox
            if (id of account of mailbox of previousDraft) is (id of targetAccount) then
                set end of beforeIds to (id of previousDraft as text)
            end if
        end repeat
        set parentMessageId to ""
        {creation}
        set sender of d to {_quoted(sender)}
        {subject_override}
        {_recipient_script("to", to)}
        {_recipient_script("cc", cc)}
        {_recipient_script("bcc", bcc)}
        set theMessage to d
        {attachment_block}
        set visible of d to true
        activate
        delay 0.5
        set resultData to {{|composer_id|:(id of d as text), |subject|:(subject of d), |before_ids|:beforeIds, |parent_message_id|:parentMessageId}}
    end tell""",
        handlers=mailbox_handlers,
    )


def _save_script(composer_id: str, before_ids: list[str], body: str, account: str) -> str:
    if not composer_id.isdecimal() or any(not value.isdecimal() for value in before_ids):
        raise NativeDraftError(
            "Invalid native composer identity; no save attempted.", composer_id=composer_id
        )
    previous = ", ".join(_quoted(value) for value in before_ids)
    return _json_script(f"""tell application "Mail"
        set d to first outgoing message whose id is {composer_id}
        save d
        delay 0.5
        set beforeIds to {{{previous}}}
        set targetAccount to {applescript_account_clause(account)}
        set matches to {{}}
        set savedSource to ""
        repeat with savedDraft in messages of drafts mailbox
            set candidateId to id of savedDraft as text
            if candidateId is not in beforeIds then
                if (id of account of mailbox of savedDraft) is (id of targetAccount) then
                    if (subject of savedDraft) is (subject of d) and (extract address from sender of savedDraft) is (extract address from sender of d) then
                        if (content of savedDraft as text) starts with {_quoted(body)} then
                            set end of matches to candidateId
                            set candidateSource to source of savedDraft
                            if candidateSource is not missing value then
                                if (count characters of candidateSource) <= 4000000 then set savedSource to candidateSource
                            end if
                        end if
                    end if
                end if
            end if
        end repeat
        set resultData to {{|candidate_ids|:matches, |source|:savedSource}}
    end tell""")


def create_native_draft(
    run: Callable[[str], str],
    *,
    account: str,
    sender: str,
    seed: str,
    seed_id: str | None,
    seed_mailbox: str | None,
    to: list[str] | None,
    cc: list[str] | None,
    bcc: list[str] | None,
    subject: str | None,
    body: str,
    reply_all: bool,
    attachment_block: str,
    mailbox_handlers: str,
) -> dict[str, Any]:
    """Preflight, create native reply, insert, save, and report verification gaps."""
    script = creation_script(
        account=account,
        sender=sender,
        seed=seed,
        seed_id=seed_id,
        seed_mailbox=seed_mailbox,
        to=to,
        cc=cc,
        bcc=bcc,
        subject=subject,
        reply_all=reply_all,
        attachment_block=attachment_block,
        mailbox_handlers=mailbox_handlers,
    )
    composer_id = ""
    draft_id = ""
    deadline = monotonic() + 45

    def request(payload: str) -> str:
        if monotonic() + 15 > deadline:
            raise NativeDraftError(
                "Native composition time budget exhausted before the next operation. Inspect the existing composer before retrying.",
                composer_id=composer_id,
                draft_id=draft_id,
            )
        return run(payload)

    if not _COMPOSE_LOCK.acquire(blocking=False):
        raise NativeDraftError(
            "Native Mail composition is busy; no draft was created. Retry only after the current composition finishes."
        )
    try:
        try:
            # The capability check MUST precede Mail's create/reply command.
            preflight = json.loads(request("COMPOSE\n" + json.dumps({"operation": "preflight"})))
            created = json.loads(request(script))
            composer_id = str(created["composer_id"])
            evidence: dict[str, Any] = {}
            if body:
                evidence = json.loads(
                    request(
                        "COMPOSE\n"
                        + json.dumps(
                            {
                                "operation": "insert_reply_text",
                                "subject": created["subject"],
                                "body": body,
                                "requires_quote": seed in {"reply", "forward"},
                                "session_token": preflight["session_token"],
                            }
                        )
                    )
                )
            saved = json.loads(
                request(_save_script(composer_id, created["before_ids"], body, account))
            )
            matches = saved.get("candidate_ids", [])
            if len(matches) != 1:
                raise NativeDraftError(
                    f"Native draft saved, but expected one matching persisted ID and found {len(matches)}. Inspect the existing composer before retrying.",
                    composer_id=composer_id,
                )
            draft_id = str(matches[0])
            inspection = inspect_message_source(
                str(saved.get("source", "")),
                expected_parent_rfc_id=created.get("parent_message_id") or None,
                expected_body=body or None,
            )
            verification = inspection.get("verification", {})
            required_checks = (["authored_body"] if body else []) + (
                ["reply_linkage"] if created.get("parent_message_id") else []
            )
            if not inspection.get("success") or any(
                verification.get("checks", {}).get(check, {}).get("status") != "passed"
                for check in required_checks
            ):
                raise NativeDraftError(
                    "DRAFT_VERIFICATION_FAILED: saved MIME did not verify authored text "
                    "outside the quotation and the expected reply headers. The draft "
                    "exists but is not ready; inspect it before retrying.",
                    composer_id=composer_id,
                    draft_id=draft_id,
                )
            return {
                "draft_id": draft_id,
                "sent_message_id": "",
                "from_account": account,
                "composer_id": composer_id,
                "composition_mode": "mail_defaults",
                "parent_message_id": created["parent_message_id"],
                "subject": created["subject"],
                "native_evidence": evidence,
                "verification_status": "body_and_reply_headers_verified",
                "verification": verification,
            }
        except NativeDraftError:
            raise
        except Exception as error:
            raise NativeDraftError(
                str(error), composer_id=composer_id, draft_id=draft_id
            ) from error
    finally:
        _COMPOSE_LOCK.release()
