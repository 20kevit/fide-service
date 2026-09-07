"""Consistent JSON error contract. No business imports."""

import uuid
from flask import jsonify, request

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
    """Build the envelope. Never includes tracebacks, DB errors, secrets."""
    body = {"error": {
        "code": code,
        "message": message,
        "details": details or {},
        "request_id": uuid.uuid4().hex,
    }}
    resp = jsonify(body)
    resp.status_code = CODE_STATUS[code]
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
