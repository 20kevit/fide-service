"""Service-to-service API-key authentication + rate limiting.

- Read endpoints require ``X-API-Key`` matching FIDE_API_KEY.
- Admin endpoints require the effective admin key (FIDE_ADMIN_KEY when
  set, otherwise FIDE_API_KEY) via ``X-Admin-Key`` or ``X-API-Key``.
- Comparison uses hmac.compare_digest. Failures fail closed (401/503).
- Keys come from settings (environment) — never hard-coded, logged, or
  returned in responses. Auth is a thin decorator layer so it can later
  evolve (rotation, scopes, signing) without touching business logic.
"""
import hmac
import time
from functools import wraps
from typing import Dict, List

from flask import current_app, g, request

from interfaces.api.errors import ApiError

#: key -> [monotonic timestamps of recent requests] (process-local).
_request_log: Dict[str, List[float]] = {}
_WINDOW_SECONDS = 60.0


def _settings():
    return current_app.config["FIDE_SETTINGS"]


def _presented_read_key() -> str:
    return (request.headers.get("X-API-Key") or "").strip()


def _presented_admin_key() -> str:
    return ((request.headers.get("X-Admin-Key")
             or request.headers.get("X-API-Key")) or "").strip()


def _check_rate_limit(key: str) -> None:
    settings = _settings()
    limit = settings.rate_limit_per_minute
    if limit <= 0:
        return  # disabled (tests / local only)
    now = time.monotonic()
    hits = _request_log.get(key, [])
    hits = [t for t in hits if now - t < _WINDOW_SECONDS]
    if len(hits) >= limit:
        retry_after = max(1, int(_WINDOW_SECONDS - (now - hits[0])))
        raise ApiError("RATE_LIMITED", "Rate limit exceeded. Slow down.",
                       headers={"Retry-After": str(retry_after)})
    hits.append(now)
    _request_log[key] = hits


def reset_rate_limits() -> None:
    """Test hook: clear the process-local counters."""
    _request_log.clear()


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
        if not _valid(_presented_read_key(), settings.api_key):
            raise ApiError("UNAUTHORIZED",
                           "Missing or invalid API key.")
        _check_rate_limit("read:" + settings.api_key)
        g.auth_scope = "read"
        return view(*args, **kwargs)

    return wrapper


def require_admin_key(view):
    """Protect an admin endpoint with the admin credential."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        settings = _settings()
        expected = settings.effective_admin_key
        if not expected:
            raise ApiError("SERVICE_UNAVAILABLE",
                           "Service is not configured to serve requests.")
        if not _valid(_presented_admin_key(), expected):
            raise ApiError("UNAUTHORIZED",
                           "Missing or invalid admin credential.")
        _check_rate_limit("admin:" + expected)
        g.auth_scope = "admin"
        return view(*args, **kwargs)

    return wrapper
