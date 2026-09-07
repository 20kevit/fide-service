"""Status (authenticated) and liveness probe (unauthenticated)."""
from flask import Blueprint, jsonify

from application import player_service
from interfaces.api.auth import require_api_key

bp = Blueprint("api_status", __name__)


@bp.get("/status")
@require_api_key
def status():
    return jsonify(player_service.get_status())


@bp.get("/healthz")
def healthz():
    # Load-balancer probe only: no dataset details, no auth.
    return jsonify({"ok": True})
