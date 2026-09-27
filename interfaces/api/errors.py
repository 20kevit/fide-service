"""Consistent JSON error contract. No business imports."""

import logging
import uuid
from flask import g, has_request_context, jsonify, request

log = logging.getLogger(__name__)

#: Stable machine-readable codes (HTTP status in parentheses).
CODE_STATUS = {
    "BAD_REQUEST": 400,
    "UNAUTHORIZED": 401,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "CONFLICT": 409,
    "RATE_LIMITED": 429,
    "SERVICE_UNAVAILABLE": 503,
    "INTERNAL": 500,
}


class ApiError(Exception):
    """Raised anywhere in the API layer; rendered as the error envelope."""

    def __init__(self, code, message, details=None, headers=None):
        if code not in CODE_STATUS:
            raise ValueError("unknown error code: %r" % (code,))
        super().__init__(message)
        self.code = code
        self.http_status = CODE_STATUS[code]
        self.message = message
        self.details = details or {}
        self.headers = headers or {}


def error_response(code, message, details=None, headers=None):
    """Build the envelope. Never includes tracebacks, DB errors, secrets.

    The ``request_id`` is shared with the per-request id assigned in the
    app factory, so auth rejections and the client-visible envelope carry
    the SAME id (see _reject in auth.py). It is also logged server-side
    (with the sanitized path, never query strings or headers) so a
    client-reported id can always be correlated to these logs. 5xx ->
    error, 429 -> warning, anything else -> info (probe/typo traffic
    must not read as a crisis).
    """
    if has_request_context():
        request_id = getattr(g, "request_id", None) or uuid.uuid4().hex
    else:  # defensive: envelope builders always run in a request
        request_id = uuid.uuid4().hex
    status = CODE_STATUS[code]
    path = ((request.path or "")[:128]
            .replace("\r", "").replace("\n", ""))
    if status >= 500:
        log.error("api error code=%s status=%d path=%s request_id=%s",
                  code, status, path, request_id)
    elif status == 429:
        log.warning("api error code=%s status=%d path=%s request_id=%s",
                    code, status, path, request_id)
    else:
        log.info("api error code=%s status=%d path=%s request_id=%s",
                 code, status, path, request_id)
    body = {"error": {
        "code": code,
        "message": message,
        "details": details or {},
        "request_id": request_id,
    }}
    resp = jsonify(body)
    resp.status_code = status
    for key, value in (headers or {}).items():
        resp.headers[key] = value
    return resp


def register_error_handlers(app):
    @app.errorhandler(ApiError)
    def _api_error(exc):
        return error_response(exc.code, exc.message, exc.details, exc.headers)

    @app.errorhandler(400)
    def _bad_request(_exc):
        return error_response("BAD_REQUEST", "Malformed request.")

    @app.errorhandler(404)
    def _not_found(_exc):
        if (request.path or "").startswith("/api/"):
            return error_response("NOT_FOUND", "No such endpoint.")
        return error_response("NOT_FOUND", "Not found.")

    @app.errorhandler(405)
    def _method_not_allowed(_exc):
        return error_response("BAD_REQUEST", "HTTP method not allowed here.")

    @app.errorhandler(413)
    def _too_large(_exc):
        return error_response("BAD_REQUEST",
                              "Request body exceeds the size limit.")

    @app.errorhandler(500)
    def _internal(_exc):
        # Generic on purpose: no traceback, no DB detail, no secrets.
        return error_response("INTERNAL",
                              "Unexpected internal error. Retry later.")
