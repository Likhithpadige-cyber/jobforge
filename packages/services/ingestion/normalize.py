"""Deterministic job normalization.

No LLM is involved anywhere in this module, by design (ARCHITECTURE.md §0.3
principle 4: deterministic plumbing is a service, not an agent). Everything
here is pure and testable without a database, which is why the pre-filter and
dedupe run before any model is ever called and keep evaluation cost down.

The functions never invent data. When a field cannot be parsed with
confidence, they return None and the field stays empty rather than being
filled with a plausible guess.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal
from packages.shared.clock import utc_now

# ---------------------------------------------------------------- constants

SENIORITY_PATTERNS: tuple[tuple[str, str], ...] = (
    # Ordered: the first match wins, so check the most specific first.
    (r"\b(principal|distinguished|fellow)\b", "lead"),
    (r"\b(staff|lead|head\s+of|manager|director)\b", "lead"),
    (r"\b(sr\.?|senior|snr)\b", "senior"),
    (r"\b(mid[\s-]?level|intermediate|ii|iii)\b", "mid"),
    (r"\b(jr\.?|junior|entry[\s-]?level|associate|graduate|intern|trainee|i)\b", "junior"),
)

#: Noise that appears in titles but carries no signal for matching.
TITLE_NOISE = (
    r"\((remote|hybrid|onsite|on-site|contract|w2|c2c|full[\s-]?time|part[\s-]?time)\)",
    r"\b(urgent|immediate\s+joiner|hot\s+job|apply\s+now|hiring\s+now)\b",
    r"[-–—|]\s*(remote|hybrid|onsite|us|usa|united\s+states)\s*$",
    r"\bw2\s+only\b",
    r"\b\d{4,}\b",  # requisition numbers
    r"#\s*\w+",
)

#: Suffixes stripped when normalizing a company name for dedupe.
COMPANY_SUFFIXES = (
    "inc", "inc.", "incorporated", "llc", "l.l.c.", "ltd", "ltd.", "limited",
    "corp", "corp.", "corporation", "co", "co.", "company", "plc", "gmbh",
    "sa", "s.a.", "nv", "n.v.", "ab", "as", "oy", "pty", "pvt", "private",
    "holdings", "group", "technologies", "technology", "labs",
)

REMOTE_PATTERNS = (
    r"\bfully\s+remote\b", r"\b100%\s+remote\b", r"\bremote\b",
    r"\bwork\s+from\s+home\b", r"\bwfh\b", r"\bdistributed\b",
)
HYBRID_PATTERNS = (r"\bhybrid\b", r"\b\d\s*days?\s+(?:in[\s-]?office|onsite|on[\s-]?site)\b")

US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}
STATE_CODES = set(US_STATES.values())

SALARY_PATTERN = re.compile(
    r"(?P<currency>[$£€])?\s*(?P<low>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(?P<lowunit>k|m)?\s*(?:-|–|—|to)\s*"
    r"(?P<currency2>[$£€])?\s*(?P<high>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(?P<highunit>k|m)?",
    re.IGNORECASE,
)
PERIOD_PATTERN = re.compile(
    r"\b(per\s+)?(?P<period>hour|hourly|hr|year|yearly|annum|annual|month|monthly|day|daily)\b",
    re.IGNORECASE,
)
CURRENCY_SYMBOLS = {"$": "USD", "£": "GBP", "€": "EUR"}

EMPLOYMENT_TYPES = (
    (r"\b(full[\s-]?time|permanent|perm)\b", "full_time"),
    (r"\b(part[\s-]?time)\b", "part_time"),
    (r"\b(contract|contractor|c2c|corp[\s-]?to[\s-]?corp|w2)\b", "contract"),
    (r"\b(intern|internship)\b", "internship"),
    (r"\b(temp|temporary)\b", "temporary"),
)

Seniority = Literal["junior", "mid", "senior", "lead", "unknown"]


# ------------------------------------------------------------------ helpers
def _collapse(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def strip_accents(value: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch)
    )


def clean_text(value: str | None) -> str | None:
    """Strip control characters, zero-width characters and HTML remnants.

    Zero-width characters matter beyond tidiness: they are a standard vector
    for hiding prompt-injection payloads in text that later reaches a model
    (SECURITY.md §5.2).
    """
    if value is None:
        return None
    value = value.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
    value = value.replace("\ufeff", "").replace("\u00ad", "")
    value = "".join(ch for ch in value if ch == "\n" or ch == "\t" or ch >= " ")
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"</p>", "\n\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", " ", value)
    value = (
        value.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        .replace("&nbsp;", " ").replace("&#39;", "'").replace("&quot;", '"')
    )
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip() or None


# ------------------------------------------------------------------- titles
def normalize_title(title: str) -> str:
    """Reduce a posting title to its comparable core.

    Used for dedupe and for the cheap pre-filter, never for display: the
    original title is always kept in `jobs.title`.
    """
    value = strip_accents(title).lower()
    for pattern in TITLE_NOISE:
        value = re.sub(pattern, " ", value, flags=re.IGNORECASE)
    value = re.sub(r"[^a-z0-9+#/\s-]", " ", value)
    value = re.sub(r"\bsr\b", "senior", value)
    value = re.sub(r"\bjr\b", "junior", value)
    value = re.sub(r"\bdevops\b", "devops", value)
    value = re.sub(r"\bsre\b", "site reliability engineer", value)
    # Removing noise words leaves dangling separators behind:
    # "URGENT: Cloud Engineer - Apply Now" would otherwise normalize to
    # "cloud engineer -", which breaks exact-title dedupe.
    value = _collapse(value)
    value = value.strip(" -–—|:,/")
    value = re.sub(r"\s+[-–—|:,]+\s*$", "", value)
    value = re.sub(r"^\s*[-–—|:,]+\s+", "", value)
    return _collapse(value)


def extract_seniority(title: str, description: str | None = None) -> Seniority:
    """Best-effort seniority. Returns 'unknown' rather than guessing."""
    haystack = normalize_title(title)
    for pattern, level in SENIORITY_PATTERNS:
        if re.search(pattern, haystack, re.IGNORECASE):
            return level  # type: ignore[return-value]
    if description:
        head = description[:600].lower()
        for pattern, level in SENIORITY_PATTERNS:
            if re.search(pattern, head, re.IGNORECASE):
                return level  # type: ignore[return-value]
    return "unknown"


def normalize_company_name(name: str) -> str:
    """Slug-like key for matching the same employer across sources."""
    value = strip_accents(name).lower()
    value = re.sub(r"[^\w\s.-]", " ", value)
    tokens = [t.strip(".") for t in _collapse(value).split()]
    while tokens and tokens[-1] in COMPANY_SUFFIXES:
        tokens.pop()
    return "-".join(t for t in tokens if t) or _collapse(value).replace(" ", "-")


# ---------------------------------------------------------------- locations
@dataclass(frozen=True)
class ParsedLocation:
    city: str | None = None
    state: str | None = None
    country: str | None = None
    is_remote: bool = False
    is_hybrid: bool = False


def parse_location(raw: str | None) -> ParsedLocation:
    """Parse a free-text location. Unparseable parts stay None."""
    if not raw:
        return ParsedLocation()

    value = _collapse(strip_accents(raw))
    lowered = value.lower()

    is_hybrid = any(re.search(p, lowered) for p in HYBRID_PATTERNS)
    is_remote = any(re.search(p, lowered) for p in REMOTE_PATTERNS) and not is_hybrid

    cleaned = re.sub(
        r"\b(fully\s+remote|100%\s+remote|remote|hybrid|onsite|on-site|"
        r"work\s+from\s+home|wfh|distributed)\b",
        "",
        lowered,
    )
    cleaned = _collapse(cleaned.strip(" ,-–|/"))
    if not cleaned:
        return ParsedLocation(is_remote=is_remote, is_hybrid=is_hybrid)

    parts = [p.strip() for p in re.split(r"[,/|]", cleaned) if p.strip()]
    city = state = country = None

    for part in parts:
        if part in US_STATES:
            state = US_STATES[part]
        elif part.upper() in STATE_CODES and len(part) == 2:
            state = part.upper()
        elif part in {"usa", "us", "united states", "united states of america"}:
            country = "US"
        elif part in {"uk", "united kingdom", "england", "scotland", "wales"}:
            country = "GB"
        elif part in {"canada", "ca"} and state is None:
            country = "CA"
        elif city is None:
            city = part.title()

    if state and country is None:
        country = "US"
    return ParsedLocation(city, state, country, is_remote, is_hybrid)


# ------------------------------------------------------------------ salary
@dataclass(frozen=True)
class ParsedSalary:
    minimum: int | None = None
    maximum: int | None = None
    currency: str | None = None
    period: str | None = None


def _to_amount(number: str, unit: str | None) -> float:
    amount = float(number.replace(",", ""))
    if unit and unit.lower() == "k":
        amount *= 1_000
    elif unit and unit.lower() == "m":
        amount *= 1_000_000
    return amount


def parse_salary(text: str | None) -> ParsedSalary:
    """Parse a salary range. Returns empty rather than guessing a single figure.

    Deliberately conservative: a single number ("competitive, around 120k")
    is ignored, because turning one number into a range would be inventing
    the other end of it.
    """
    if not text:
        return ParsedSalary()

    match = SALARY_PATTERN.search(text)
    if not match:
        return ParsedSalary()

    low = _to_amount(match.group("low"), match.group("lowunit"))
    high = _to_amount(match.group("high"), match.group("highunit"))

    # "120-150k" means both ends are thousands.
    if match.group("highunit") and not match.group("lowunit") and low < 1000:
        low = _to_amount(match.group("low"), match.group("highunit"))

    if high < low:
        low, high = high, low

    symbol = match.group("currency") or match.group("currency2")
    currency = CURRENCY_SYMBOLS.get(symbol) if symbol else None
    if currency is None and re.search(r"\b(usd|dollars?)\b", text, re.IGNORECASE):
        currency = "USD"

    period_match = PERIOD_PATTERN.search(text)
    period = None
    if period_match:
        raw_period = period_match.group("period").lower()
        period = {
            "hour": "hourly", "hourly": "hourly", "hr": "hourly",
            "year": "yearly", "yearly": "yearly", "annum": "yearly", "annual": "yearly",
            "month": "monthly", "monthly": "monthly",
            "day": "daily", "daily": "daily",
        }[raw_period]
    elif high <= 500:
        period = "hourly"
    elif high >= 10_000:
        period = "yearly"

    # Reject implausible parses rather than storing nonsense.
    if period == "yearly" and (high < 10_000 or high > 2_000_000):
        return ParsedSalary()
    if period == "hourly" and high > 2_000:
        return ParsedSalary()

    return ParsedSalary(int(low), int(high), currency, period)


def extract_employment_type(title: str, description: str | None = None) -> str | None:
    haystack = f"{title} {(description or '')[:800]}".lower()
    for pattern, value in EMPLOYMENT_TYPES:
        if re.search(pattern, haystack):
            return value
    return None


# ------------------------------------------------------------------ hashing
def content_hash(title: str, company: str | None, description: str | None) -> str:
    """Stable hash of a posting's substance, for exact-duplicate detection.

    Built from normalized parts so that whitespace, casing and boilerplate
    noise do not produce a different hash for the same posting.
    """
    parts = [
        normalize_title(title),
        normalize_company_name(company) if company else "",
        _collapse(re.sub(r"[^a-z0-9 ]", " ", (clean_text(description) or "").lower()))[:4000],
    ]
    return hashlib.sha256("\u241f".join(parts).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------- result
@dataclass
class NormalizedJob:
    """Output of normalization. Mirrors the writable columns of `jobs`."""

    title: str
    url: str
    normalized_title: str
    company_name: str | None = None
    normalized_company: str | None = None
    external_id: str | None = None
    seniority: str = "unknown"
    location_raw: str | None = None
    location_city: str | None = None
    location_state: str | None = None
    location_country: str | None = None
    is_remote: bool = False
    is_hybrid: bool = False
    employment_type: str | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str | None = None
    salary_period: str | None = None
    description_raw: str | None = None
    description_hash: str | None = None
    posted_at: datetime | None = None
    expires_at: datetime | None = None
    warnings: list[str] = field(default_factory=list)


def normalize_job(
    *,
    title: str,
    url: str,
    company_name: str | None = None,
    external_id: str | None = None,
    location: str | None = None,
    description: str | None = None,
    salary_text: str | None = None,
    posted_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> NormalizedJob:
    """Normalize one raw posting. Pure: no I/O, no model, no database."""
    warnings: list[str] = []

    title = _collapse(title)
    if not title:
        raise ValueError("job title cannot be empty")
    if not url:
        raise ValueError("job url cannot be empty")

    description_clean = clean_text(description)
    parsed_location = parse_location(location)
    salary = parse_salary(salary_text or description_clean)

    if location and not any(
        (parsed_location.city, parsed_location.state, parsed_location.country,
         parsed_location.is_remote, parsed_location.is_hybrid)
    ):
        warnings.append(f"could not parse location: {location!r}")
    if salary_text and salary.minimum is None:
        warnings.append("salary text present but not parseable; left empty")

    normalized_company = normalize_company_name(company_name) if company_name else None

    return NormalizedJob(
        title=title,
        url=url,
        normalized_title=normalize_title(title),
        company_name=_collapse(company_name) if company_name else None,
        normalized_company=normalized_company,
        external_id=external_id,
        seniority=extract_seniority(title, description_clean),
        location_raw=location,
        location_city=parsed_location.city,
        location_state=parsed_location.state,
        location_country=parsed_location.country,
        is_remote=parsed_location.is_remote,
        is_hybrid=parsed_location.is_hybrid,
        employment_type=extract_employment_type(title, description_clean),
        salary_min=salary.minimum,
        salary_max=salary.maximum,
        salary_currency=salary.currency,
        salary_period=salary.period,
        description_raw=description_clean,
        description_hash=content_hash(title, company_name, description_clean),
        posted_at=posted_at,
        expires_at=expires_at,
        warnings=warnings,
    )


def is_stale(posted_at: datetime | None, max_age_days: int = 60) -> bool:
    """Postings older than the window are not worth an LLM call."""
    if posted_at is None:
        return False
    reference = posted_at if posted_at.tzinfo else posted_at.replace(tzinfo=UTC)
    return utc_now() - reference > timedelta(days=max_age_days)
