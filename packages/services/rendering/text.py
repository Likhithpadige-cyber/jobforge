"""Shared, deterministic text policy for preview and both export formats."""

from __future__ import annotations

import unicodedata
from urllib.parse import urlsplit


def normalise_text(value: str) -> str:
    """NFC preserves meaning; expand tabs so PDF and DOCX cannot disagree."""
    return unicodedata.normalize("NFC", value).replace("\t", " ")


def single_line(value: str) -> str:
    if any(c in value for c in ("\n", "\r", "\t", "\v", "\f", "\u2028", "\u2029")):
        raise ValueError("Export contact and structured fields must be single-line")
    return unicodedata.normalize("NFC", value)


def export_url(value: str) -> str:
    value = single_line(value)
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(c.isspace() or ord(c) < 32 for c in value)
    ):
        raise ValueError(
            "Export links require an absolute HTTP(S) URL without whitespace or credentials"
        )
    _ = parsed.port  # Also reject malformed/non-numeric ports.
    return value
