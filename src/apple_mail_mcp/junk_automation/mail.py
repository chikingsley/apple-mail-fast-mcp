"""Small Apple Mail domain layer built on the signed helper."""

from __future__ import annotations

from typing import Any

from .bridge import encoded, preamble, rows, run

READ_LIMIT = 20
UPDATE_LIMIT = 100
RECIPIENT_LIMIT = 25
INVENTORY_LIMIT = 10_000
JUNK_MAILBOX_NAMES = frozenset({"Junk Email", "Junk Mail", "Spam"})


def _message_filter(ids: list[str]) -> str:
    if any(value.isdigit() is False for value in ids):
        raise ValueError("Use numeric Mail IDs returned by search_messages")
    return " or ".join(f"id is {int(value)}" for value in ids) or "false"


def _account(value: str) -> str:
    return f"""set accountKey to {encoded(value)}
set matches to every account whose name is accountKey
if (count of matches) is 0 then set matches to every account whose id is accountKey
if (count of matches) is 0 then error "Unknown Mail account: " & accountKey
set targetAccount to item 1 of matches
"""


def _mailbox(value: str, variable: str = "targetMailbox") -> str:
    fallback = value.rsplit("/", maxsplit=1)[-1]
    return f"""set mailboxKey to {encoded(value)}
set matches to every mailbox of targetAccount whose name is mailboxKey
if (count of matches) is 0 then
  set mailboxKey to {encoded(fallback)}
  set matches to every mailbox of targetAccount whose name is mailboxKey
end if
if (count of matches) is 0 then error "Unknown mailbox: " & mailboxKey
set {variable} to item 1 of matches
"""


class Mail:
    """Expose the Mail.app operations that agents actually use."""

    def __init__(self) -> None:
        self._account_cache: list[dict[str, Any]] | None = None

    def accounts(self) -> list[dict[str, Any]]:
        script = (
            preamble()
            + """
tell application id "com.apple.mail"
  set output to ""
  set accountItems to every account
  set accountIds to id of every account
  set accountNames to name of every account
  set accountEnabled to enabled of every account
  set accountAddresses to email addresses of every account
  repeat with position from 1 to count of accountItems
    set oldDelimiters to AppleScript's text item delimiters
    set AppleScript's text item delimiters to character id 31
    set addresses to (item position of accountAddresses) as text
    set AppleScript's text item delimiters to oldDelimiters
    set output to output & my emit({item position of accountIds, item position of accountNames, item position of accountEnabled, addresses})
  end repeat
end tell
return output
"""
        )
        accounts = [
            {
                "id": row[0],
                "name": row[1],
                "enabled": row[2] == "true",
                "addresses": row[3].split(chr(31)) if row[3] else [],
            }
            for row in rows(run(script))
        ]
        self._account_cache = accounts
        return accounts

    def account_id(self, value: str) -> str:
        """Resolve an account name or ID to its stable Mail account ID."""
        accounts = self._account_cache or self.accounts()
        key = value.casefold()
        for account in accounts:
            if str(account["id"]).casefold() == key or str(account["name"]).casefold() == key:
                return str(account["id"])
        # Mail accounts can be added while the MCP process is running. A cached miss
        # must therefore be checked against Mail.app before it is reported as unknown.
        accounts = self.accounts()
        for account in accounts:
            if str(account["id"]).casefold() == key or str(account["name"]).casefold() == key:
                return str(account["id"])
        raise ValueError(f"Unknown Mail account: {value}")

    def mailboxes(self, account: str) -> list[dict[str, Any]]:
        script = (
            preamble()
            + 'tell application id "com.apple.mail"\n'
            + _account(account)
            + """
set output to ""
set boxes to every mailbox of targetAccount
set boxNames to name of every mailbox of targetAccount
set unreadCounts to unread count of every mailbox of targetAccount
repeat with position from 1 to count of boxes
  set output to output & my emit({item position of boxNames, item position of unreadCounts})
end repeat
end tell
return output
"""
        )
        return [{"name": row[0], "unread": int(row[1])} for row in rows(run(script))]

    def junk_mailboxes(self) -> list[dict[str, str]]:
        """Discover the exact enabled account/mailbox pairs used for Junk."""
        discovered = []
        for account in self.accounts():
            if not account["enabled"]:
                continue
            account_id = str(account["id"])
            account_name = str(account["name"])
            for mailbox in self.mailboxes(account_id):
                mailbox_name = str(mailbox["name"])
                if mailbox_name in JUNK_MAILBOX_NAMES:
                    discovered.append(
                        {
                            "account_id": account_id,
                            "account_name": account_name,
                            "mailbox": mailbox_name,
                        }
                    )
        return discovered

    def junk_messages(self, mailboxes: list[dict[str, str]]) -> list[dict[str, Any]]:
        """Read message metadata from the exact Junk account/folder inventory."""
        messages = []
        for mailbox in mailboxes:
            account_id = mailbox["account_id"]
            mailbox_name = mailbox["mailbox"]
            messages.extend(
                row | {"account_id": account_id, "mailbox": mailbox_name}
                for row in self.search(account_id, mailbox_name, limit=INVENTORY_LIMIT)
            )
        return messages

    def rules(self) -> list[dict[str, Any]]:
        script = (
            preamble()
            + """
tell application id "com.apple.mail"
  set output to ""
  set ruleItems to every rule
  set ruleNames to name of every rule
  set ruleEnabled to enabled of every rule
  repeat with position from 1 to count of ruleItems
    set output to output & my emit({position, item position of ruleNames, item position of ruleEnabled})
  end repeat
end tell
return output
"""
        )
        return [
            {"index": int(row[0]), "name": row[1], "enabled": row[2] == "true"}
            for row in rows(run(script))
        ]

    def set_rule(self, index: int, *, enabled: bool | None = None, delete: bool = False) -> None:
        if index < 1:
            raise ValueError("Rule index starts at 1")
        action = (
            "delete ruleItem" if delete else f"set enabled of ruleItem to {str(enabled).lower()}"
        )
        script = (
            preamble()
            + f"""
tell application id "com.apple.mail"
  if {index} > count of rules then error "Unknown Mail rule index"
  set ruleItem to rule {index}
  {action}
end tell
return ""
"""
        )
        run(script)

    def search(
        self,
        account: str,
        mailbox: str,
        *,
        sender: str | None = None,
        subject: str | None = None,
        unread: bool | None = None,
        flagged: bool | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, INVENTORY_LIMIT))
        checks = []
        if sender:
            checks.append(f"sender contains {encoded(sender)}")
        if subject:
            checks.append(f"subject contains {encoded(subject)}")
        if unread is not None:
            checks.append(f"read status is {str(not unread).lower()}")
        if flagged is not None:
            checks.append(f"flagged status is {str(flagged).lower()}")
        query = f" whose {' and '.join(checks)}" if checks else ""
        script = preamble() + 'tell application id "com.apple.mail"\n' + _account(account)
        script += (
            _mailbox(mailbox)
            + f"""
set output to ""
set internalIds to id of (every message of targetMailbox{query})
set internetIds to message id of (every message of targetMailbox{query})
set subjectsList to subject of (every message of targetMailbox{query})
set sendersList to sender of (every message of targetMailbox{query})
set receivedDates to date received of (every message of targetMailbox{query})
set readStates to read status of (every message of targetMailbox{query})
set flagStates to flagged status of (every message of targetMailbox{query})
set attachmentLists to mail attachments of (every message of targetMailbox{query})
set resultCount to count of internalIds
if resultCount > {limit} then set resultCount to {limit}
repeat with position from 1 to resultCount
  set attachmentCount to count of item position of attachmentLists
  set output to output & my emit({{item position of internalIds, item position of internetIds, item position of subjectsList, item position of sendersList, item position of receivedDates, item position of readStates, item position of flagStates, attachmentCount}})
end repeat
end tell
return output
"""
        )
        return [self._message_row(row) for row in rows(run(script))]

    def messages(self, account: str, mailbox: str, ids: list[str]) -> list[dict[str, Any]]:
        if len(ids) > READ_LIMIT:
            raise ValueError("A request can read up to 20 messages")
        wanted = _message_filter(ids)
        script = preamble() + 'tell application id "com.apple.mail"\n' + _account(account)
        script += (
            _mailbox(mailbox)
            + f"""
set output to ""
repeat with messageItem in (every message of targetMailbox whose {wanted})
  set bodyText to content of messageItem as text
  if length of bodyText > 100000 then set bodyText to text 1 thru 100000 of bodyText
  set output to output & my emit({{id of messageItem, message id of messageItem, subject of messageItem, sender of messageItem, date received of messageItem, read status of messageItem, flagged status of messageItem, count of mail attachments of messageItem, bodyText}})
end repeat
end tell
return output
"""
        )
        decoded = rows(run(script))
        return [self._message_row(row) | {"body": row[8]} for row in decoded]

    def update(
        self,
        account: str,
        mailbox: str,
        ids: list[str],
        *,
        read: bool | None = None,
        flagged: bool | None = None,
        move_to: str | None = None,
        delete: bool = False,
    ) -> int:
        if len(ids) > UPDATE_LIMIT:
            raise ValueError("A request can update up to 100 messages")
        wanted = _message_filter(ids)
        actions = []
        if read is not None:
            actions.append(f"set read status of messageItem to {str(read).lower()}")
        if flagged is not None:
            actions.append(f"set flagged status of messageItem to {str(flagged).lower()}")
        if move_to:
            actions.append("move messageItem to destinationMailbox")
        if delete:
            actions.append("delete messageItem")
        if not actions:
            raise ValueError("Choose read, flagged, move_to, or delete")
        script = preamble() + 'tell application id "com.apple.mail"\n' + _account(account)
        script += _mailbox(mailbox)
        if move_to:
            script += _mailbox(move_to, "destinationMailbox")
        script += f"""set changed to 0
set targetMessages to every message of targetMailbox whose {wanted}
repeat with messageItem in targetMessages
  {chr(10).join(actions)}
  set changed to changed + 1
end repeat
end tell
return changed as text
"""
        return int(run(script))

    def compose(
        self,
        account: str,
        to: list[str],
        subject: str,
        body: str,
        *,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        send: bool = False,
    ) -> str:
        if not to or len(to) + len(cc or []) + len(bcc or []) > RECIPIENT_LIMIT:
            raise ValueError("Compose requires 1 to 25 recipients")
        recipient_lines = []
        for kind, values in (("to", to), ("cc", cc or []), ("bcc", bcc or [])):
            recipient_lines.extend(
                f"make new {kind} recipient at end of {kind} recipients with properties {{address:{encoded(value)}}}"
                for value in values
            )
        finish = "send outgoing" if send else "save outgoing"
        script = (
            preamble()
            + 'tell application id "com.apple.mail"\n'
            + _account(account)
            + f"""
set outgoing to make new outgoing message with properties {{subject:{encoded(subject)}, content:{encoded(body)}, visible:false}}
set senderAddresses to email addresses of targetAccount
set sender of outgoing to item 1 of senderAddresses
tell outgoing
  {chr(10).join(recipient_lines)}
end tell
{finish}
set resultId to id of outgoing as text
end tell
return resultId
"""
        )
        return run(script).strip()

    def clean_flagged_junk(self) -> dict[str, Any]:
        junk_query = " or ".join(
            f"name is {encoded(name)}"
            for name in ("Junk", "Junk Email", "Junk Mail", "Spam", "[Gmail]/Spam")
        )
        script = (
            preamble()
            + f"""
tell application id "com.apple.mail"
  set output to ""
  set cleared to 0
  set deletedCount to 0
  repeat with accountItem in every account whose enabled is true
    set boxes to every mailbox of accountItem whose {junk_query}
    repeat with box in boxes
      set flaggedMessages to every message of box whose flagged status is true
      repeat with messageItem in flaggedMessages
        set internalId to id of messageItem
        set internetId to message id of messageItem
        set output to output & my emit({{id of accountItem, name of box, internalId, internetId}})
        set flagged status of messageItem to false
        set cleared to cleared + 1
        delete messageItem
        set deletedCount to deletedCount + 1
      end repeat
    end repeat
  end repeat
end tell
return output
"""
        )
        processed = [
            {"account_id": row[0], "mailbox": row[1], "id": row[2], "message_id": row[3]}
            for row in rows(run(script, timeout=180))
        ]
        return {
            "messages": processed,
            "flags_cleared": len(processed),
            "moved_to_trash": len(processed),
        }

    @staticmethod
    def _message_row(row: list[str]) -> dict[str, Any]:
        return {
            "id": row[0],
            "message_id": row[1],
            "subject": row[2],
            "sender": row[3],
            "received": row[4],
            "read": row[5] == "true",
            "flagged": row[6] == "true",
            "attachments": int(row[7]),
        }
