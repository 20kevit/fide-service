"""FIDE error taxonomy. Pure Python — no framework imports.

Category names are kept identical to the proven Swiss implementation so
operational knowledge transfers; human messages are English because this
service exposes a JSON API (no admin HTML UI). Every failure converts to
a classified FideError with a stage key, a safe detail string (never
secrets), and a suggested operator action.
"""
import re


class FideErrorCategory:
    NETWORK_DNS_ERROR = "NETWORK_DNS_ERROR"
    NETWORK_CONNECTION_ERROR = "NETWORK_CONNECTION_ERROR"
    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
    NETWORK_TLS_ERROR = "NETWORK_TLS_ERROR"
    HTTP_ERROR = "HTTP_ERROR"
    HTTP_RATE_LIMIT = "HTTP_RATE_LIMIT"
    INVALID_CONTENT = "INVALID_CONTENT"
    INVALID_XML = "INVALID_XML"
    INVALID_FIDE_FORMAT = "INVALID_FIDE_FORMAT"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    FILE_READ_ERROR = "FILE_READ_ERROR"
    PARSER_ERROR = "PARSER_ERROR"
    DATABASE_ERROR = "DATABASE_ERROR"
    ALREADY_RUNNING = "ALREADY_RUNNING"
    ALREADY_IMPORTED = "ALREADY_IMPORTED"
    WORKER_STALE = "WORKER_STALE"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


STAGE_LABELS = {
    "dns": "DNS",
    "connect": "connect",
    "tls": "TLS",
    "http": "HTTP request",
    "download": "download",
    "extract": "extract",
    "validate": "validate",
    "parse": "parse XML",
    "save": "persist",
    "verify": "verify",
    "finalize": "finalize",
    "upload": "upload",
}

CATEGORY_MESSAGES = {
    FideErrorCategory.NETWORK_DNS_ERROR: (
        "DNS resolution failed",
        "Check the server DNS settings or stage the monthly ZIP manually.",
    ),
    FideErrorCategory.NETWORK_CONNECTION_ERROR: (
        "Cannot reach the FIDE download host",
        "Check outbound connectivity or stage the monthly ZIP manually.",
    ),
    FideErrorCategory.NETWORK_TIMEOUT: (
        "Connection timed out",
        "Retry later or stage the monthly ZIP manually.",
    ),
    FideErrorCategory.NETWORK_TLS_ERROR: (
        "TLS connection failed",
        "Check TLS/CA configuration or stage the monthly ZIP manually.",
    ),
    FideErrorCategory.HTTP_ERROR: (
        "FIDE host returned an HTTP error",
        "See the technical detail for the status code; retry later.",
    ),
    FideErrorCategory.HTTP_RATE_LIMIT: (
        "Rate limited by the FIDE host",
        "Wait a few minutes and retry.",
    ),
    FideErrorCategory.INVALID_CONTENT: (
        "Downloaded content is not a valid archive",
        "Retry later or stage a known-good ZIP manually.",
    ),
    FideErrorCategory.INVALID_XML: (
        "File is not valid XML",
        "Provide a different file from the official FIDE source.",
    ),
    FideErrorCategory.INVALID_FIDE_FORMAT: (
        "File structure does not match the FIDE format",
        "Provide a different file from the official FIDE source.",
    ),
    FideErrorCategory.FILE_TOO_LARGE: (
        "File exceeds the size limit",
        "Stage the file server-side instead of uploading it.",
    ),
    FideErrorCategory.FILE_NOT_FOUND: (
        "File not found",
        "Check the file name and path, then retry.",
    ),
    FideErrorCategory.FILE_READ_ERROR: (
        "Cannot read file",
        "Check file permissions and disk health.",
    ),
    FideErrorCategory.PARSER_ERROR: (
        "Failed to process data",
        "Nothing was persisted for the failed batch; try another file.",
    ),
    FideErrorCategory.DATABASE_ERROR: (
        "Database error",
        "Changes were rolled back; check database health and retry.",
    ),
    FideErrorCategory.ALREADY_RUNNING: (
        "Import already running",
        "Another worker owns this step; check the jobs status.",
    ),
    FideErrorCategory.ALREADY_IMPORTED: (
        "Period already imported",
        "This period completed successfully; retry is not needed.",
    ),
    FideErrorCategory.WORKER_STALE: (
        "Worker stopped without heartbeat",
        "The step can be safely retried explicitly.",
    ),
    FideErrorCategory.UNKNOWN_ERROR: (
        "Unexpected error",
        "Check the technical detail and retry; escalate on recurrence.",
    ),
}


def public_error_summary(category) -> str:
    """Safe one-line failure summary for API exposure.

    Built only from the curated ``CATEGORY_MESSAGES`` table — never from
    exception text, SQL, paths, or player data. The raw detail stays
    server-side (worker logs); API clients get code + summary + advice.
    Unknown/missing categories map to the generic entry.
    """
    title, advice = CATEGORY_MESSAGES.get(
        category, CATEGORY_MESSAGES[FideErrorCategory.UNKNOWN_ERROR])
    return "%s. %s" % (title, advice)


class FideError(Exception):
    """Classified failure: category + stage + safe detail + advice."""

    def __init__(self, category, stage="download", detail="", advice=None):
        self.category = category
        self.stage = stage
        self.detail = detail or ""
        title, default_advice = CATEGORY_MESSAGES.get(
            category, CATEGORY_MESSAGES[FideErrorCategory.UNKNOWN_ERROR]
        )
        self.title = title
        self.advice = advice if advice is not None else default_advice
        super().__init__(self.summary())

    def summary(self):
        parts = [self.title]
        if self.detail:
            parts.append(str(self.detail))
        return " | ".join(parts)

    def to_dict(self):
        return {
            "category": self.category,
            "stage": self.stage,
            "stage_label": STAGE_LABELS.get(self.stage, self.stage),
            "title": self.title,
            "detail": self.detail,
            "advice": self.advice,
        }


_HTTP_CODE_RE = re.compile(r"\b[45]\d{2}\b")


def _has_http_code(blob: str) -> bool:
    """True for standalone 4xx/5xx codes. Word boundaries keep MySQL
    error numbers like 1406 (or 2003) from matching as HTTP statuses."""
    return bool(_HTTP_CODE_RE.search(blob))


def classify_exception(exc):
    """Best-effort mapping of arbitrary exceptions to a category."""
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    blob = name + " " + text
    if "fideerror" in name:
        return None  # already classified; caller keeps it as-is
    if any(k in blob for k in ("dns", "getaddrinfo", "name or service", "nodename")):
        return FideErrorCategory.NETWORK_DNS_ERROR
    if any(k in blob for k in ("ssl", "tls", "certificate", "handshake")):
        return FideErrorCategory.NETWORK_TLS_ERROR
    if any(k in blob for k in ("timeout", "timed out", "deadline")):
        return FideErrorCategory.NETWORK_TIMEOUT
    if any(k in blob for k in ("connection", "connect", "refused", "unreachable",
                               "reset by peer", "network is unreachable")):
        return FideErrorCategory.NETWORK_CONNECTION_ERROR
    if any(k in blob for k in ("429", "rate limit", "too many requests")):
        return FideErrorCategory.HTTP_RATE_LIMIT
    # Database failures first: driver/ORM markers ("1406" alone must NOT
    # match the HTTP branch below — it is a MySQL error number, not a
    # status code).
    if any(k in blob for k in ("sqlalchemy", "pymysql", "mysqldb",
                               "integrityerror", "dataerror",
                               "operationalerror", "programmingerror",
                               "internalerror", "database is locked")):
        return FideErrorCategory.DATABASE_ERROR
    if "http" in blob or "status" in blob or _has_http_code(blob):
        return FideErrorCategory.HTTP_ERROR
    if any(k in blob for k in ("not well-formed", "mismatched tag", "no element found",
                               "parseerror", "xml")):
        return FideErrorCategory.INVALID_XML
    if any(k in blob for k in ("too large", "exceeds", "413", "entity too large")):
        return FideErrorCategory.FILE_TOO_LARGE
    if any(k in blob for k in ("no such file", "not found", "enoent")):
        return FideErrorCategory.FILE_NOT_FOUND
    return FideErrorCategory.UNKNOWN_ERROR
