"""Campaign evidence normalization for messages already classified as Junk."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from email.utils import parseaddr

MIN_NUMERIC_LENGTH = 8
MIN_SEPARATED_LENGTH = 10
MIN_SEPARATED_DIGITS = 4
MIN_LONG_LENGTH = 16
MIN_LONG_DIGITS = 6
MIN_LONG_LETTERS = 3


@dataclass(frozen=True)
class CampaignPolicy:
    """Evidence thresholds for rotating-domain campaign recognition."""

    minimum_domains: int = 3
    minimum_messages: int = 3
    observation_window_days: int = 30
    minimum_qualifying_cycles: int = 2


DEFAULT_POLICY = CampaignPolicy()


@dataclass(frozen=True)
class SenderFingerprint:
    """Normalized sender identity used to correlate rotating domains."""

    local_part: str
    domain: str


def normalize_sender_email(sender: str) -> str:
    """Extract an exact lower-case address, including malformed junk domains."""
    _, address = parseaddr(sender)
    if "@" not in address:
        match = re.search(r"<\s*([^<>\s]+@[^<>\s]+)\s*>", sender)
        address = match.group(1) if match else ""
    local_part, separator, domain = address.rpartition("@")
    local_part = local_part.strip().lower()
    domain = domain.strip().lower().rstrip(".")
    if separator != "@" or not local_part or not domain:
        return ""
    return f"{local_part}@{domain}"


def fingerprint_sender(sender: str) -> SenderFingerprint | None:
    """Extract a normalized local-part and domain from a Mail sender value."""
    address = normalize_sender_email(sender)
    local_part, separator, domain = address.rpartition("@")
    if separator != "@" or not local_part or "." not in domain:
        return None
    return SenderFingerprint(local_part=local_part, domain=domain)


def _words(value: str, *, replace_numbers: bool = False) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    if replace_numbers:
        value = re.sub(r"\b\d+(?:[.,]\d+)*\b", " number ", value)
    value = "".join(character if character.isalnum() else " " for character in value)
    return " ".join(value.split())


def normalize_display_name(sender: str) -> str:
    """Return a comparison key for the sender's visible display name."""
    return _words(parseaddr(sender)[0])


def normalize_subject(subject: str) -> str:
    """Return a comparison key that ignores punctuation and changing numbers."""
    return _words(subject, replace_numbers=True)


def looks_generated_local_part(local_part: str) -> bool:
    """Recognize machine-generated mailbox names with conservative shape rules."""
    length = len(local_part)
    digits = sum(character.isdigit() for character in local_part)
    separators = sum(not character.isalnum() for character in local_part)
    letters = sum(character.isalpha() for character in local_part)
    return (
        (length >= MIN_NUMERIC_LENGTH and digits == length)
        or (length >= MIN_SEPARATED_LENGTH and digits >= MIN_SEPARATED_DIGITS and separators >= 1)
        or (length >= MIN_LONG_LENGTH and digits >= MIN_LONG_DIGITS and letters >= MIN_LONG_LETTERS)
    )
