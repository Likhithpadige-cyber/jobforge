"""Deterministic prompt minimization and blocking G2 validation.

Only allowlisted B1 fields reach the prompt builder. Scrub recognizable contact
spans and sensitive clauses there, then independently check the final serialized
request at the client boundary. Ambiguous contact hints fail closed instead of
sending a best-effort redaction. Errors never contain the matched private text.
"""

from __future__ import annotations

import html
import re
import unicodedata

from packages.agents.base.injection import strip_hidden_characters
from packages.shared.errors import ValidationError
from packages.shared.logging import redact_processor

REDACTED = "[REDACTED_CONTACT]"


class PiiInPromptError(ValidationError):
    """Residual private data blocks the provider call."""


_CONTACT = re.compile(
    r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|"
    r"(?<!\w)(?:\+?\d[\d ().-]{8,}\d)(?!\w)|"
    r"\b\d{1,6}\s+[^\n,;.!?]{1,60}\s"
    r"(?:street|st|road|rd|avenue|ave|lane|ln|drive|dr|boulevard|blvd|"
    r"court|ct|close|circle|cir|way|terrace|place|pl|parkway|pkwy)\b"
    r"(?:,?\s+(?:unit|apt|apartment|suite|#)\s*[\w-]+)?",
    re.IGNORECASE,
)
_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
)
_BIRTH_DATE = re.compile(
    r"\b(?:date of birth|dob|born(?: on)?|birthday)\s*[:=-]?\s*(?:"
    r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}|" + _MONTH + r"\s+\d{1,2}(?:st|nd|rd|th)?[,]?\s+\d{4}|"
    r"\d{1,2}\s+" + _MONTH + r"\s+\d{4})",
    re.IGNORECASE,
)
_SENSITIVE = re.compile(
    r"\b(date of birth|dob|born|birthday|work authori[sz]ation|"
    r"visa|sponsorship|social security|ssn|(?:home|residential|mailing) address)\b",
    re.IGNORECASE,
)
_CLAUSE = re.compile(r"[^.!?;\n]+")
# Wider detection than automatic scrubbing: when safe span boundaries are not
# obvious, require correction upstream rather than guessing what to remove.
_RESIDUAL_HINT = re.compile(
    r"\b(?:born|birth\s*date|birthdate|birthday|dob|home address|residential address|"
    r"mailing address|work authori[sz]ation|visa|sponsorship|social security|ssn)\b|"
    r"\b(?:I live|my home is|reside)\s+(?:at\s+)?\d|"
    r"\bP\.?\s*O\.?\s+Box\s+\d|"
    r"[\w.+-]+\s*\[at\]\s*[\w.-]+\s*\[dot\]\s*\w+",
    re.IGNORECASE,
)
_IDENTIFIER = re.compile(
    r"\b(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|" r"[0-9a-f]{64})\b",
    re.IGNORECASE,
)


def _normalize(value: str) -> str:
    return strip_hidden_characters(unicodedata.normalize("NFKC", value))[0]


def redact_prompt_text(value: str) -> str:
    """Redact contact spans/sensitive clauses, retaining unrelated job text."""
    value = _normalize(value)
    value = redact_processor(None, "", {"text": value})["text"]
    value = _BIRTH_DATE.sub(REDACTED, value)
    value = _CONTACT.sub(REDACTED, value)
    return _CLAUSE.sub(lambda match: REDACTED if _SENSITIVE.search(match[0]) else match[0], value)


def assert_prompt_safe(*parts: str) -> None:
    """Check the actual outbound text, including HTML/JSON escaped content."""
    for part in parts:
        decoded = html.unescape(part)
        decoded = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), decoded)
        decoded = _normalize(decoded)
        # UUIDs and SHA-256 digests are identifiers, not telephone numbers.
        decoded = _IDENTIFIER.sub("", decoded)
        if _CONTACT.search(decoded) or _RESIDUAL_HINT.search(decoded):
            raise PiiInPromptError("Unredacted private data detected in outgoing prompt")
        if redact_processor(None, "", {"text": decoded})["text"] != decoded:
            raise PiiInPromptError("Unredacted secret detected in outgoing prompt")
