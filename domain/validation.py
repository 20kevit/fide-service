"""Domain-level input validation. Pure Python — no framework imports."""

from domain.models import RATING_TYPES

#: FIDE IDs are numeric in practice, but the official files are the
#: authority: the service requires non-empty, bounded IDs and never
#: invents stricter rules that could reject real data.
FIDE_ID_MAX_LENGTH = 20
FEDERATION_MAX_LENGTH = 5


def validate_fide_id(value) -> str:
    """Normalize + validate a FIDE ID. Returns the stripped ID.

    Raises ValueError when missing/empty/too long. Inside the FIDE
    Service the ID is ALWAYS required (unlike Swiss, where it is
    nullable) — it is the primary business identity.
    """
    cleaned = (value or "").strip() if isinstance(value, str) else ""
    if not cleaned:
        raise ValueError("fide_id is required")
    if len(cleaned) > FIDE_ID_MAX_LENGTH:
        raise ValueError(
            "fide_id must be at most %d characters" % FIDE_ID_MAX_LENGTH
        )
    return cleaned


def validate_rating_type(value: str) -> str:
    """Validate a rating type (standard|rapid|blitz)."""
    if value not in RATING_TYPES:
        raise ValueError("rating_type must be one of %s" % (RATING_TYPES,))
    return value


def validate_period(value: str) -> str:
    """Validate a dataset period (YYYY-MM, year >= 2000)."""
    try:
        year_s, mon_s = value.split("-")
        year, month = int(year_s), int(mon_s)
    except (ValueError, AttributeError):
        raise ValueError("period must be YYYY-MM, got %r" % (value,))
    if not 1 <= month <= 12 or year < 2000 or len(year_s) != 4:
        raise ValueError("period must be YYYY-MM, got %r" % (value,))
    return "%04d-%02d" % (year, month)


def normalize_federation(value) -> str:
    """Normalize an optional federation filter to upper-case code."""
    if value is None:
        return ""
    code = value.strip().upper()
    if len(code) > FEDERATION_MAX_LENGTH:
        raise ValueError("federation must be at most %d characters"
                         % FEDERATION_MAX_LENGTH)
    return code
