"""
Custom exceptions for Apple Mail MCP operations.
"""


class MailError(Exception):
    """Base exception for Mail operations."""


class MailAccountNotFoundError(MailError):
    """Account does not exist."""


class MailMailboxNotFoundError(MailError):
    """Mailbox does not exist."""


class MailMailboxNotEmptyError(MailError):
    """Mailbox cannot be deleted because it contains messages and the
    caller did not opt in to cascade-delete via ``delete_messages=True``.
    """


class MailUnsupportedGmailSystemLabelError(MailError):
    """Operation targets a Gmail system label (the ``[Gmail]`` parent or
    any ``[Gmail]/...`` child path).

    Gmail's IMAP server does not support normal RENAME/DELETE semantics
    for these paths — renames may silently revert and deletes are
    refused. Tracked in #164; future Gmail-label-CRUD tools (sub-feature
    2 of #164) will provide a proper alternative.
    """


class MailImapRequiredError(MailError):
    """The requested operation requires IMAP credentials and the user
    hasn't opted in (no Keychain entry, or entry is unreachable). Surfaces
    the gap so the caller can prompt the user to set up IMAP if they want
    the operation.
    """


class MailAttachmentIndexError(MailError):
    """The requested attachment index doesn't exist on the message
    (out of range, or the message has no attachments). (#250)
    """


class MailAttachmentTooLargeError(MailError):
    """The attachment exceeds the inline-content size cap for
    ``get_attachment_content``; the caller should use ``save_attachments``
    for large files. (#250)
    """


class MailImapMoveUnsupportedError(MailError):
    """The IMAP server advertises neither MOVE (RFC 6851) nor UIDPLUS
    (RFC 4315). No safe scoped move is possible; the orchestrator must
    fall back to AppleScript. A non-UIDPLUS unscoped EXPUNGE would
    remove every \\Deleted-flagged message in the mailbox, not just the
    ones we just moved.
    """


class MailImapTrashNotFoundError(MailError):
    """The IMAP server doesn't advertise a \\Trash SPECIAL-USE folder
    (RFC 6154) and no folder matching the conventional names (Trash,
    [Gmail]/Trash, Deleted Messages, Deleted Items) was found. Without
    a Trash folder we can't preserve the move-to-Trash semantic of
    delete_messages — fall back to AppleScript.
    """


class MailMessageNotFoundError(MailError):
    """Message does not exist."""


class MailAppleScriptError(MailError):
    """AppleScript execution failed."""


class MailPermissionError(MailError):
    """Permission denied for operation."""


class MailOperationCancelledError(MailError):
    """User cancelled the operation."""


class MailSafetyError(MailError):
    """Safety check failed in test mode (wrong account or non-reserved recipient)."""


class MailKeychainError(MailError):
    """Keychain operation failed."""


class MailKeychainEntryNotFoundError(MailKeychainError):
    """Requested Keychain entry does not exist.

    Expected and benign: signals the user has not opted in to IMAP
    for this account. Delegation layer (future work) treats this as
    a silent fall-back-to-AppleScript signal.
    """


class MailKeychainAccessDeniedError(MailKeychainError):
    """Keychain refused access (ACL denied or user denied prompt).

    Worth surfacing to the user on first failure per the graceful-
    degradation invariants in imap-auth-options-decision.md.
    """


class MailRuleNotFoundError(MailError):
    """Rule index is out of range — no such rule exists in Mail.app."""


class MailUnsupportedRuleActionError(MailError):
    """update_rule was called on a rule whose existing actions include
    one that's not modeled in our schema (e.g. run-AppleScript,
    redirect, reply, play sound). Read access via list_rules is
    unaffected; only mutating an existing rule with these actions is
    refused.
    """


class MailDraftError(MailError):
    """Base class for draft-lifecycle errors."""


class MailDraftInvalidIdError(MailDraftError):
    """Draft id failed validation (path traversal, invalid chars, too long,
    or empty). Ids must match ^[A-Za-z0-9._@+=-]{1,255}$ — a Mail.app
    numeric id or a bare RFC 5322 Message-ID, with no path separators.
    """


class MailDraftNotFoundError(MailDraftError):
    """No draft exists with the requested id (lookup across Drafts mailboxes
    of every account returned nothing).
    """


class MailDraftHtmlUnavailableError(MailDraftError):
    """An HTML draft (``body_html``) was requested but the clean IMAP-APPEND
    path could not run (no Keychain opt-in / IMAP credentials, breaker open,
    or APPEND failed). HTML drafts are built as RFC822 multipart/alternative
    over IMAP — Mail.app's AppleScript ``content`` setter is plain-text only —
    so we fail loud rather than silently downgrade to a plain-text draft.
    (#251)
    """


class MailTemplateError(MailError):
    """Base class for email-template errors."""


class MailTemplateNotFoundError(MailTemplateError):
    """No template exists with the requested name."""


class MailTemplateInvalidNameError(MailTemplateError):
    """Template name fails validation (path traversal, invalid chars,
    too long, or empty). Names must match ^[a-zA-Z0-9_-]{1,64}$.
    """


class MailTemplateInvalidFormatError(MailTemplateError):
    """A file in the templates directory could not be parsed as a
    template (malformed header, unreadable, or empty body).
    """


class MailTemplateMissingVariableError(MailTemplateError):
    """render_template encountered a {placeholder} with no matching
    auto-fill or user-supplied variable. The exception message names
    the missing placeholder(s).
    """
