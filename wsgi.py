"""Passenger/cPanel entry point (production deployment phase only).

Kept minimal and inert during local development: it only exposes the
WSGI application object. No server is configured in Phase 1.
"""
from run import app as application  # noqa: F401
