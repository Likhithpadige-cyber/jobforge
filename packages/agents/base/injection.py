"""Prompt-injection input scanner (SECURITY.md §5.2 control 2, AGENT_DESIGN.md G1).

A job description is attacker-controlled text that we feed to a model which
then writes documents in the candidate's name. This module is the first of
the seven layers that defend that path.

**It never rejects a posting.** SECURITY.md is explicit about why: refusing to
read suspicious postings would be trivially exploitable as a denial of
service - an attacker could hide the candidate from every job on a board by
salting the descriptions. Detection produces findings, the findings raise
`guardrail.violation.detected` with kind `prompt_injection`, the job is
flagged for human view, and processing continues with the text treated as
data.

The scanner is deterministic and contains no model call. It is deliberately
one layer of several: an injection that evades every pattern here still has
to produce a claim that survives citation, the numeral guard, an independent
verifier, and a human reading a claim table.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum


class Severity(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class FindingKind(StrEnum):
    INSTRUCTION_OVERRIDE = "instruction_override"
    ROLE_MARKER = "role_marker"
    DELIMITER_ESCAPE = "delimiter_escape"
    FABRICATION_DIRECTIVE = "fabrication_directive"
    EXFILTRATION_ATTEMPT = "exfiltration_attempt"
    HIDDEN_CHARACTERS = "hidden_characters"
    ENCODED_PAYLOAD = "encoded_payload"


#: (kind, severity, pattern, why it matters)
PATTERNS: tuple[tuple[FindingKind, Severity, re.Pattern[str], str], ...] = (
    (
        FindingKind.INSTRUCTION_OVERRIDE,
        Severity.HIGH,
        re.compile(
            r"\b(ignore|disregard|forget|override|bypass)\b[^.\n]{0,40}"
            r"\b(previous|prior|above|earlier|all|any|your)\b[^.\n]{0,20}"
            r"\b(instruction|prompt|rule|direction|guideline|system)s?\b",
            re.IGNORECASE,
        ),
        "attempts to discard the system prompt",
    ),
    (
        FindingKind.ROLE_MARKER,
        Severity.HIGH,
        re.compile(
            r"(^|\n)\s*(system|assistant|user)\s*:|"
            r"\[/?INST\]|<\|im_(start|end)\|>|<\|(system|assistant|user)\|>|"
            r"###\s*(system|assistant|instruction)",
            re.IGNORECASE,
        ),
        "forges a conversation turn boundary",
    ),
    (
        FindingKind.DELIMITER_ESCAPE,
        Severity.HIGH,
        re.compile(
            r"</?(untrusted|job_description|context|system|prompt)>",
            re.IGNORECASE,
        ),
        "tries to close the delimiter wrapping untrusted data",
    ),
    (
        FindingKind.FABRICATION_DIRECTIVE,
        Severity.HIGH,
        re.compile(
            r"\b(state|say|claim|write|assert|add|include|pretend|report)\b"
            r"[^.\n]{0,40}\bthat\b[^.\n]{0,60}"
            r"\b(candidate|applicant|they|he|she|you)\b[^.\n]{0,40}"
            r"\b(has|have|holds?|is|are|possesses?|earned|completed)\b",
            re.IGNORECASE,
        ),
        "instructs the model to assert something about the candidate",
    ),
    (
        FindingKind.FABRICATION_DIRECTIVE,
        Severity.HIGH,
        re.compile(
            r"\b(rate|score|rank|grade)\b[^.\n]{0,30}" r"\b(100|perfect|maximum|highest|top)\b",
            re.IGNORECASE,
        ),
        "instructs the model to manipulate the fit score",
    ),
    (
        FindingKind.EXFILTRATION_ATTEMPT,
        Severity.HIGH,
        re.compile(
            r"\b(list|output|print|reveal|repeat|show|send|email|include)\b"
            r"[^.\n]{0,40}\b(your\s+)?(system\s+prompt|instructions|api[\s_-]?key|"
            r"credential|password|personal\s+(details|information)|phone\s+number|"
            r"home\s+address|full\s+profile)\b",
            re.IGNORECASE,
        ),
        "attempts to extract prompt contents or personal data",
    ),
    (
        FindingKind.ENCODED_PAYLOAD,
        Severity.MEDIUM,
        re.compile(r"(?:[A-Za-z0-9+/]{40,}={0,2})"),
        "long base64-like run that may hide an instruction",
    ),
    (
        FindingKind.ENCODED_PAYLOAD,
        Severity.MEDIUM,
        re.compile(r"(?:\\u[0-9a-fA-F]{4}){4,}"),
        "escaped-unicode run that may hide an instruction",
    ),
)

#: Characters that render as nothing but survive copy/paste. A standard way
#: to hide an instruction from a human reviewer while a model still reads it.
HIDDEN_CHARS = {
    "\u200b": "zero-width space",
    "\u200c": "zero-width non-joiner",
    "\u200d": "zero-width joiner",
    "\u2060": "word joiner",
    "\ufeff": "byte-order mark",
    "\u00ad": "soft hyphen",
    "\u202a": "left-to-right embedding",
    "\u202b": "right-to-left embedding",
    "\u202d": "left-to-right override",
    "\u202e": "right-to-left override",
}


@dataclass(frozen=True)
class Finding:
    kind: FindingKind
    severity: Severity
    excerpt: str
    reason: str
    position: int


@dataclass
class ScanResult:
    """Outcome of a scan. `text` is always returned - never withheld."""

    text: str
    findings: list[Finding] = field(default_factory=list)
    hidden_characters_removed: int = 0

    @property
    def clean(self) -> bool:
        return not self.findings

    @property
    def severity(self) -> Severity | None:
        if not self.findings:
            return None
        for level in (Severity.HIGH, Severity.MEDIUM, Severity.LOW):
            if any(f.severity is level for f in self.findings):
                return level
        return None

    @property
    def requires_human_review(self) -> bool:
        """Any high-severity finding flags the job for a human to look at.

        It does not stop processing - see the module docstring.
        """
        return any(f.severity is Severity.HIGH for f in self.findings)

    def as_payload(self) -> dict[str, object]:
        """Shape for a `guardrail.violation.detected` event payload."""
        return {
            "kind": "prompt_injection",
            "severity": str(self.severity) if self.severity else None,
            "finding_count": len(self.findings),
            "kinds": sorted({str(f.kind) for f in self.findings}),
            "hidden_characters_removed": self.hidden_characters_removed,
            # Excerpts are short and quoted so a reviewer can see what fired
            # without the full posting being copied into the event log.
            "excerpts": [f.excerpt for f in self.findings[:5]],
        }


def _excerpt(text: str, start: int, end: int, width: int = 60) -> str:
    left = max(0, start - 10)
    right = min(len(text), end + width)
    snippet = text[left:right].replace("\n", " ")
    return re.sub(r"\s+", " ", snippet).strip()[:120]


def strip_hidden_characters(text: str) -> tuple[str, int]:
    """Remove invisible characters, returning the count removed.

    Also strips Unicode format characters (category Cf) generally, so a
    variant not in HIDDEN_CHARS still cannot survive.
    """
    removed = 0
    out: list[str] = []
    for char in text:
        if char in HIDDEN_CHARS or (unicodedata.category(char) == "Cf" and char not in "\n\t"):
            removed += 1
            continue
        out.append(char)
    return "".join(out), removed


def scan(text: str | None) -> ScanResult:
    """Scan untrusted text for injection patterns.

    Returns the (de-obfuscated) text alongside the findings. The caller
    always receives usable text: this function has no rejection path.
    """
    if not text:
        return ScanResult(text=text or "")

    cleaned, removed = strip_hidden_characters(text)
    findings: list[Finding] = []

    if removed:
        findings.append(
            Finding(
                kind=FindingKind.HIDDEN_CHARACTERS,
                severity=Severity.MEDIUM,
                excerpt=f"{removed} invisible character(s)",
                reason="invisible characters can hide instructions from a human reviewer",
                position=0,
            )
        )

    # Scan the de-obfuscated text: an injection split by zero-width characters
    # must not evade the patterns simply by being unreadable.
    for kind, severity, pattern, reason in PATTERNS:
        for match in pattern.finditer(cleaned):
            findings.append(
                Finding(
                    kind=kind,
                    severity=severity,
                    excerpt=_excerpt(cleaned, match.start(), match.end()),
                    reason=reason,
                    position=match.start(),
                )
            )

    return ScanResult(text=cleaned, findings=findings, hidden_characters_removed=removed)
