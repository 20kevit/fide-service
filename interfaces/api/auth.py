"""Service-to-service API-key authentication + rate limiting.

- Read endpoints require ``X-API-Key`` matching FIDE_API_KEY.
- Admin endpoints require the effective admin key (FIDE_ADMIN_KEY when
  set, otherwise FIDE_API_KEY) via ``X-Admin-Key`` or ``X-API-Key``.
- Comparison uses hmac.compare_digest. Failures fail closed (401/503).
- Keys come from settings (environment) — never hard-coded, logged, or
  returned in responses. Auth is a thin decorator layer so it can later
  evolve (rotation, scopes, signing) without touching business logic.
- Rate limiting is two-layer, both process-local and bounded (this
  service runs behind multi-process Passenger, so in-app counters are
  per-process defense-in-depth; a gateway global cap is documented in
  the runbook as the production outer layer):
  1. post-auth per-key sliding window (``RATE_LIMIT_PER_MINUTE``);
  2. pre-auth per-IP rejected-attempt budget
     (``AUTH_FAIL_LIMIT_PER_HOUR``) — the 401/403 path never reaches
     layer 1, so without layer 2 key-guessing would be unthrottled.
- Security events (401/403 rejections, 429 transitions) are logged with
  a key fingerprint (SHA-256 prefix — irreversible, correlation only),
  request path (sanitized), and direct peer IP. Raw credentials, query
  strings, and headers are never logged. Only the block *transition* is
  logged, so log volume stays bounded even under flood.
"""

import hashlib
import hmac
import logging
import time
from functools import wraps
from typing import Dict, List

from flask import current_app, g, request

from interfaces.api.errors import ApiError, CODE_STATUS

log = logging.getLogger(__name__)

#: key -> [monotonic timestamps of recent requests] (process-local).
#: Bounded (see _check_rate_limit): stale keys are purged so memory
#: cannot grow without bound regardless of key cardinality.
_request_log: Dict[str, List[float]] = {}
_WINDOW_SECONDS = 60.0
_MAX_TRACKED_KEYS = 5000

#: bucket -> monotonic time of the last logged 429 transition.
#: Blocked hits never append to _request_log, so without this marker
#: every excess hit would look like a new transition and flood the log.
_rate_logged: Dict[str, float] = {}

#: ip -> [monotonic timestamps of rejected auth attempts] (process-local).
#: Same bounding discipline as _request_log; window is one hour.
_fail_log: Dict[str, List[float]] = {}
_FAIL_WINDOW_SECONDS = 3600.0
_MAX_TRACKED_IPS = 5000


def _settings():
    return current_app.config["FIDE_SETTINGS"]


def _presented_read_key() -> str:
    return (request.headers.get("X-API-Key") or "").strip()


def _presented_admin_key() -> str:
    return ((request.headers.get("X-Admin-Key")
             or request.headers.get("X-API-Key")) or "").strip()


def _client_ip() -> str:
    """Direct peer address only. ``X-Forwarded-For`` and friends are
    deliberately ignored — they are client-controlled and spoofable."""
    return (request.remote_addr or "unknown")[:64]


def _key_fingerprint(presented: str) -> str:
    """Short irreversible fingerprint for log correlation (never the key).

    SHA-256 over the presented value, first 12 hex chars. Preimage-safe
    for the long random keys this service issues; ``"none"`` when empty.
    """
    if not presented:
        return "none"
    return hashlib.sha256(presented.encode("utf-8", "ignore")).hexdigest()[:12]


def _safe_path() -> str:
    """Request path for logs: bounded, no newlines, no query string."""
    return ((request.path or "")[:128]
            .replace("\r", "").replace("\n", ""))


def _prune(hits: List[float], now: float, window: float) -> List[float]:
    return [t for t in hits if now - t < window]


def _bounded_put(store: Dict[str, List[float]], key: str,
                 hits: List[float], limit_keys: int, window: float,
                 now: float) -> None:
    store[key] = hits
    if len(store) > limit_keys:
        # Opportunistic purge: drop entries with no fresh hits. Runs at
        # most once per limit_keys inserts (amortized cheap).
        stale = [k for k, ts in store.items()
                 if not ts or now - ts[-1] >= window]
        for k in stale:
            del store[k]


def _check_rate_limit(key: str, key_fp: str) -> None:
    settings = _settings()
    limit = settings.rate_limit_per_minute
    if limit <= 0:
        return  # disabled (tests / local only)
    now = time.monotonic()
    hits = _prune(_request_log.get(key, []), now, _WINDOW_SECONDS)
    if len(hits) >= limit:
        if now - _rate_logged.get(key, 0.0) >= _WINDOW_SECONDS:
            # Transition only: further blocked hits in this window stay
            # silent so a flood cannot flood the logs.
            _rate_logged[key] = now
            log.warning("rate limit exceeded ip=%s key_fp=%s path=%s "
                        "status=429", _client_ip(), key_fp, _safe_path())
        retry_after = max(1, int(_WINDOW_SECONDS - (now - hits[0])))
        raise ApiError("RATE_LIMITED", "Rate limit exceeded. Slow down.",
                       headers={"Retry-After": str(retry_after)})
    hits.append(now)
    _bounded_put(_request_log, key, hits, _MAX_TRACKED_KEYS,
                 _WINDOW_SECONDS, now)


def _preauth_blocked() -> bool:
    """True when this IP already exhausted its rejected-attempt budget."""
    settings = _settings()
    limit = settings.auth_fail_limit_per_hour
    if limit <= 0:
        return False  # disabled (tests / local only)
    now = time.monotonic()
    ip = _client_ip()
    hits = _prune(_fail_log.get(ip, []), now, _FAIL_WINDOW_SECONDS)
    _bounded_put(_fail_log, ip, hits, _MAX_TRACKED_IPS,
                 _FAIL_WINDOW_SECONDS, now)
    return len(hits) > limit


def _record_auth_failure() -> bool:
    """Count one rejected attempt for this IP.

    Returns True exactly when this hit newly exceeds the budget (the
    caller logs that transition; later blocked hits stay silent).
    """
    settings = _settings()
    limit = settings.auth_fail_limit_per_hour
    if limit <= 0:
        return False
    now = time.monotonic()
    ip = _client_ip()
    hits = _prune(_fail_log.get(ip, []), now, _FAIL_WINDOW_SECONDS)
    hits.append(now)
    _bounded_put(_fail_log, ip, hits, _MAX_TRACKED_IPS,
                 _FAIL_WINDOW_SECONDS, now)
    return len(hits) == limit + 1


def _reject(code: str, message: str, event: str, presented: str) -> None:
    """Log a safe rejection record, then raise the public error.

    Never logs credentials, query strings, or headers — only the event,
    peer IP, key fingerprint, sanitized path, status, and the shared
    per-request id (assigned in the app factory; the error envelope the
    client receives carries this same id).
    """
    tripped = _record_auth_failure()
    log.warning("auth rejected event=%s ip=%s key_fp=%s path=%s "
                "status=%d over_budget=%s request_id=%s",
                event, _client_ip(), _key_fingerprint(presented),
                _safe_path(), CODE_STATUS[code], tripped,
                getattr(g, "request_id", "-"))
    raise ApiError(code, message)


def _preauth_gate() -> None:
    """Reject already-over-budget IPs before any key comparison.

    Silent by design: the trip itself was logged by _reject when the
    budget was exhausted, so repeated blocks add no log volume.
    """
    if _preauth_blocked():
        raise ApiError("RATE_LIMITED",
                       "Too many failed attempts. Slow down.",
                       headers={"Retry-After": "60"})


def reset_rate_limits() -> None:
    """Test hook: clear the process-local counters."""
    _request_log.clear()
    _fail_log.clear()
    _rate_logged.clear()


def _valid(presented: str, expected: str) -> bool:
    if not expected or not presented:
        return False
    return hmac.compare_digest(presented, expected)


def require_api_key(view):
    """Protect a read endpoint with the service API key."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        settings = _settings()
        if not settings.api_key:
            raise ApiError("SERVICE_UNAVAILABLE",
                           "Service is not configured to serve requests.")
        _preauth_gate()
        presented = _presented_read_key()
        if not _valid(presented, settings.api_key):
            _reject("UNAUTHORIZED", "Missing or invalid API key.",
                    "bad_read_key", presented)
        _check_rate_limit("read:" + settings.api_key,
                          _key_fingerprint(settings.api_key))
        g.auth_scope = "read"
        return view(*args, **kwargs)

    return wrapper


def require_admin_key(view):
    """Protect an admin endpoint with the admin credential.

    A valid READ key presented here is authenticated but not authorized
    -> 403 FORBIDDEN (not 401): the caller is known, the scope is not.
    Missing/invalid credentials -> 401. Unconfigured service -> 503.
    """

    @wraps(view)
    def wrapper(*args, **kwargs):
        settings = _settings()
        expected = settings.effective_admin_key
        if not expected:
            raise ApiError("SERVICE_UNAVAILABLE",
                           "Service is not configured to serve requests.")
        _preauth_gate()
        presented = _presented_admin_key()
        if _valid(presented, expected):
            _check_rate_limit("admin:" + expected,
                              _key_fingerprint(expected))
            g.auth_scope = "admin"
            return view(*args, **kwargs)
        if _valid(_presented_read_key(), settings.api_key):
            _reject("FORBIDDEN",
                    "Admin credential required for this endpoint.",
                    "read_key_on_admin", presented)
        _reject("UNAUTHORIZED", "Missing or invalid admin credential.",
                "bad_admin_key", presented)

    return wrapper
