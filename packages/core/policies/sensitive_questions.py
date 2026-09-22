"""The hard-coded sensitive-question classifier (AGENT_DESIGN.md §4.2).

A regex/keyword pre-classifier, not a model judgement. It runs before any
grounding is attempted, and it has exactly one power: escalate a question to
`sensitive`. It has no way to de-escalate one - there is no branch here, or
anywhere a caller of this module can reach, that takes a `sensitive`
classification back down. A model may be consulted for classes this module
does not recognise, but it can only ever raise the bar this module already
set, never lower it - see `classify` in
`packages/core/services/applications.py`, which composes this classifier
with everything else and enforces that ordering.

The list mirrors AGENT_DESIGN.md §4.2 exactly: work authorization, visa or
sponsorship, security clearance, criminal history, disability, veteran
status, race/ethnicity, gender, age, salary history. Matching is deliberately
broad (prefer a false positive - an ordinary question wrongly flagged
sensitive, costing one unnecessary human look - over a false negative, which
is the one failure mode this module exists to prevent).
"""

from __future__ import annotations

import re

#: Each pattern is matched case-insensitively against the full question text.
#: Grouped by topic only for readability; classification does not distinguish
#: which pattern matched, because the result is the same for all of them.
_SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        # Work authorization / visa / sponsorship.
        r"\bwork authoriz",
        r"\blegally authorized to work\b",
        r"\bvisa\b",
        r"\bsponsor(ship)?\b",
        r"\brequire.*sponsorship\b",
        r"\bgreen card\b",
        r"\bcitizenship\b",
        r"\b(are you a|us|u\.s\.) citizen\b",
        # Security clearance.
        r"\bsecurity clearance\b",
        r"\bclassified\b",
        r"\bpolygraph\b",
        # Criminal history.
        r"\bcriminal (record|history|background)\b",
        r"\bconvicted\b",
        r"\bfelony\b",
        r"\bmisdemeanor\b",
        r"\barrest(ed)?\b",
        # Disability.
        r"\bdisabilit",
        r"\bhandicap\b",
        r"\breasonable accommodation\b",
        # Veteran status.
        r"\bveteran\b",
        r"\bmilitary (service|discharge)\b",
        r"\bprotected veteran\b",
        # Race / ethnicity.
        r"\brace\b",
        r"\bethnicity\b",
        r"\bhispanic or latino\b",
        # Gender / sex.
        r"\bgender\b",
        r"\b(sex|pronoun)\b",
        r"\btransgender\b",
        # Age / date of birth.
        r"\bdate of birth\b",
        r"\byour age\b",
        r"\bhow old are you\b",
        r"\bbirth ?date\b",
        # Salary history (distinct from a forward-looking expectation, which
        # is `judgement`, not `sensitive` - AGENT_DESIGN.md §4.2 lists both,
        # and only history is in the hard-coded set).
        r"\b(current|previous|prior|past|recent|last) salary\b",
        r"\bsalary history\b",
        r"\bcurrent(ly)? (earn|paid|compensat)\b",
        r"\bcompensation history\b",
    )
)


def is_sensitive(question_text: str) -> bool:
    """True if the deterministic classifier recognises this as sensitive.

    Escalate-only: a caller may use this to move a question INTO `sensitive`
    classification, never to justify moving one out. There is no
    corresponding `is_not_sensitive` - the absence of a match here means
    only "this classifier does not recognise it", not "this is safe to
    auto-answer"; the caller's own classification of everything else is a
    separate, narrower allow-list (see `classify`).
    """
    return any(pattern.search(question_text) for pattern in _SENSITIVE_PATTERNS)
