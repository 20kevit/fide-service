"""Read API: players, search, ratings, history, batch."""
from flask import Blueprint, jsonify, request

from application import player_service
from application.player_service import (
    BATCH_MAX_IDS, HISTORY_MAX_LIMIT, SEARCH_MAX_LIMIT, get_dataset,
    rating_to_dict,
)
from domain.models import RATING_TYPES
from domain.validation import (
    validate_fide_id, validate_period, validate_rating_type,
)
from infrastructure.repositories import (
    FidePlayerRepository, FideRatingRepository,
)
from interfaces.api.auth import require_api_key
from interfaces.api.errors import ApiError

bp = Blueprint("api_players", __name__)


def _parse_bool(value, default=True) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y")


@bp.get("/players/<fide_id>")
@require_api_key
def get_player(fide_id):
    try:
        include = request.args.get("include", "ratings")
        payload = player_service.get_player(
            fide_id, include_ratings=(include != "none"))
    except ValueError as exc:
        raise ApiError("BAD_REQUEST", str(exc))
    if payload is None:
        raise ApiError("NOT_FOUND",
                       "Player %s not found." % (fide_id or "").strip())
    return jsonify(payload)


@bp.get("/players/search")
@require_api_key
def search_players():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        raise ApiError("BAD_REQUEST",
                       "Query parameter 'q' is required "
                       "(at least 2 characters).")
    federation = request.args.get("federation", "")
    try:
        limit = int(request.args.get("limit", 20))
    except (TypeError, ValueError):
        raise ApiError("BAD_REQUEST", "Parameter 'limit' must be an integer.")
    if not 1 <= limit <= SEARCH_MAX_LIMIT:
        raise ApiError("BAD_REQUEST",
                       "Parameter 'limit' must be between 1 and %d."
                       % SEARCH_MAX_LIMIT)
    try:
        payload = player_service.search_players(
            q, federation=federation, limit=limit)
    except ValueError as exc:
        raise ApiError("BAD_REQUEST", str(exc))
    return jsonify(payload)


@bp.get("/players/<fide_id>/ratings")
@require_api_key
def get_ratings(fide_id):
    rating_type = (request.args.get("type") or "").strip()
    period = (request.args.get("period") or "").strip()
    try:
        if period:
            if rating_type:
                validate_rating_type(rating_type)
            fid = validate_fide_id(fide_id)
            validate_period(period)
            if FidePlayerRepository.get_by_fide_id(fid) is None:
                raise ApiError("NOT_FOUND",
                               "Player %s not found." % fid)
            types = [rating_type] if rating_type else list(RATING_TYPES)
            snapshots = {}
            for t in types:
                snapshots[t] = rating_to_dict(
                    FideRatingRepository.get(fid, period, t))
            return jsonify({"data": {"fide_id": fid, "ratings": snapshots,
                                     "period": period},
                            "dataset": get_dataset()})
        payload = player_service.get_latest_ratings(
            fide_id, rating_type=rating_type)
    except ValueError as exc:
        raise ApiError("BAD_REQUEST", str(exc))
    if payload is None:
        raise ApiError("NOT_FOUND",
                       "Player %s not found." % (fide_id or "").strip())
    return jsonify(payload)


@bp.get("/players/<fide_id>/ratings/history")
@require_api_key
def get_history(fide_id):
    rating_type = (request.args.get("type") or "").strip()
    period_from = (request.args.get("from") or "").strip()
    period_to = (request.args.get("to") or "").strip()
    try:
        limit = int(request.args.get("limit", 200))
        offset = int(request.args.get("offset", 0))
    except (TypeError, ValueError):
        raise ApiError("BAD_REQUEST",
                       "Parameters 'limit'/'offset' must be integers.")
    if not 1 <= limit <= HISTORY_MAX_LIMIT:
        raise ApiError("BAD_REQUEST",
                       "Parameter 'limit' must be between 1 and %d."
                       % HISTORY_MAX_LIMIT)
    if offset < 0:
        raise ApiError("BAD_REQUEST", "Parameter 'offset' must be >= 0.")
    try:
        payload = player_service.get_rating_history(
            fide_id, rating_type=rating_type, period_from=period_from,
            period_to=period_to, limit=limit, offset=offset)
    except ValueError as exc:
        raise ApiError("BAD_REQUEST", str(exc))
    if payload is None:
        raise ApiError("NOT_FOUND",
                       "Player %s not found." % (fide_id or "").strip())
    return jsonify(payload)


@bp.post("/players/batch")
@require_api_key
def batch_lookup():
    if not request.is_json:
        raise ApiError("BAD_REQUEST", "Request body must be JSON.")
    body = request.get_json(silent=True) or {}
    ids = body.get("ids", body.get("fide_ids"))
    if not isinstance(ids, list) or not ids:
        raise ApiError("BAD_REQUEST",
                       "Body must contain a non-empty 'ids' list.")
    if len(ids) > BATCH_MAX_IDS:
        raise ApiError("BAD_REQUEST",
                       "At most %d ids per request." % BATCH_MAX_IDS)
    include_ratings = _parse_bool(body.get("include_ratings", True))
    try:
        payload = player_service.batch_lookup(
            ids, include_ratings=include_ratings)
    except ValueError as exc:
        raise ApiError("BAD_REQUEST", str(exc))
    return jsonify(payload)
