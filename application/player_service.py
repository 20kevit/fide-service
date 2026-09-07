"""Read-side queries: player, search, latest ratings, history, batch.

Returns plain JSON-ready dicts. Every data payload carries a ``dataset``
block so consumers always know which FIDE period the values came from.
"""
from typing import Dict, List, Optional

from domain.models import RATING_TYPES
from domain.titles import canonical_title
from domain.validation import (
    normalize_federation, validate_fide_id, validate_period,
    validate_rating_type,
)
from infrastructure.database import db
from infrastructure.models import FideImportJobModel, FidePlayerModel
from infrastructure.repositories import (
    FidePlayerRepository, FideRatingRepository,
)

DATASET_SOURCE = "fide-official-xml"
SEARCH_DEFAULT_LIMIT = 20
SEARCH_MAX_LIMIT = 100
HISTORY_DEFAULT_LIMIT = 200
HISTORY_MAX_LIMIT = 1000
BATCH_MAX_IDS = 100


def rating_to_dict(row) -> Optional[Dict]:
    if row is None:
        return None
    return {"rating": row.rating, "games": row.games,
            "k_factor": row.k_factor, "period": row.period}


def get_dataset() -> Dict:
    """Service dataset version: latest period holding data + import time."""
    period = FideRatingRepository.latest_period()
    imported_at = None
    if period:
        row = (db.session.query(FideImportJobModel.completed_at)
               .filter(FideImportJobModel.period == period)
               .filter(FideImportJobModel.state == "completed")
               .order_by(FideImportJobModel.completed_at.desc())
               .first())
        if row and row[0]:
            imported_at = row[0].isoformat() + "Z"
    return {"period": period, "imported_at": imported_at,
            "source": DATASET_SOURCE}


def player_to_dict(player: FidePlayerModel,
                   latest: Dict, dataset: Dict) -> Dict:
    return {
        "fide_id": player.fide_id,
        "name": player.name,
        "federation": player.federation or "",
        "sex": player.sex or "",
        "title": player.title or "",
        "wtitle": player.wtitle or "",
        "otitle": player.otitle or "",
        "foatitle": player.foatitle or "",
        "canonical_title": canonical_title(
            player.title, player.wtitle, player.otitle, player.foatitle),
        "birth_year": player.birth_year or "",
        "inactive": bool(player.inactive),
        "latest": {t: rating_to_dict(latest.get(t)) for t in RATING_TYPES},
    }


def get_player(fide_id: str, include_ratings: bool = True) -> Optional[Dict]:
    """One player + latest ratings (None when unknown)."""
    fid = validate_fide_id(fide_id)
    player = FidePlayerRepository.get_by_fide_id(fid)
    if player is None:
        return None
    latest = (FideRatingRepository.latest_for_player(fid)
              if include_ratings
              else {t: None for t in RATING_TYPES})
    return {"data": player_to_dict(player, latest, get_dataset()),
            "dataset": get_dataset()}


def search_players(query: str, federation: str = "",
                   limit: int = SEARCH_DEFAULT_LIMIT) -> Dict:
    """Bounded search. Short/empty queries return [] (never 400 here —
    the HTTP layer decides the floor)."""
    fed = normalize_federation(federation)
    try:
        lim = int(limit)
    except (TypeError, ValueError):
        raise ValueError("limit must be an integer")
    lim = max(1, min(lim, SEARCH_MAX_LIMIT))
    q = (query or "").strip()
    rows = FidePlayerRepository.search(q, federation=fed, limit=lim) if q else []
    ids = [p.fide_id for p in rows]
    latest_map = FideRatingRepository.latest_for_players(ids)
    dataset = get_dataset()
    data = [player_to_dict(
        p, {t: latest_map.get((p.fide_id, t)) for t in RATING_TYPES}, dataset)
        for p in rows]
    return {"data": data,
            "pagination": {"limit": lim, "offset": 0, "total": len(data)},
            "dataset": dataset}


def get_latest_ratings(fide_id: str,
                       rating_type: str = "") -> Optional[Dict]:
    """Latest snapshot(s) for one player (None when the player is unknown)."""
    fid = validate_fide_id(fide_id)
    if rating_type:
        validate_rating_type(rating_type)
    if FidePlayerRepository.get_by_fide_id(fid) is None:
        return None
    latest = FideRatingRepository.latest_for_player(fid)
    types = [rating_type] if rating_type else list(RATING_TYPES)
    return {"data": {
        "fide_id": fid,
        "ratings": {t: rating_to_dict(latest.get(t)) for t in types}},
        "dataset": get_dataset()}


def get_rating_history(fide_id: str, rating_type: str = "",
                       period_from: str = "", period_to: str = "",
                       limit: int = HISTORY_DEFAULT_LIMIT,
                       offset: int = 0) -> Optional[Dict]:
    """Ascending history with optional filters (None when player unknown)."""
    fid = validate_fide_id(fide_id)
    if rating_type:
        validate_rating_type(rating_type)
    if period_from:
        validate_period(period_from)
    if period_to:
        validate_period(period_to)
    try:
        lim = max(1, min(int(limit), HISTORY_MAX_LIMIT))
        off = max(0, int(offset))
    except (TypeError, ValueError):
        raise ValueError("limit/offset must be integers")
    if FidePlayerRepository.get_by_fide_id(fid) is None:
        return None
    rows = FideRatingRepository.history(
        fid, rating_type=rating_type, period_from=period_from,
        period_to=period_to, limit=lim, offset=off)
    grouped: Dict[str, List[Dict]] = {t: [] for t in RATING_TYPES}
    for row in rows:
        if row.rating_type in grouped:
            grouped[row.rating_type].append(rating_to_dict(row))
    out = grouped if not rating_type else {rating_type: grouped[rating_type]}
    return {"data": {"fide_id": fid, "history": out},
            "pagination": {"limit": lim, "offset": off},
            "dataset": get_dataset()}


def batch_lookup(fide_ids: List[str],
                 include_ratings: bool = True) -> Dict:
    """Bulk lookup: {found: [...], missing: [...]}, max BATCH_MAX_IDS."""
    if not isinstance(fide_ids, list) or not fide_ids:
        raise ValueError("ids must be a non-empty list")
    if len(fide_ids) > BATCH_MAX_IDS:
        raise ValueError("at most %d ids per request" % BATCH_MAX_IDS)
    cleaned, seen = [], set()
    for raw in fide_ids:
        try:
            fid = validate_fide_id(raw)
        except ValueError:
            continue  # invalid entries land in `missing` via absence
        if fid not in seen:
            seen.add(fid)
            cleaned.append(fid)
    if not cleaned:
        raise ValueError("ids must contain at least one valid FIDE ID")
    players = FidePlayerRepository.get_by_fide_ids(cleaned)
    latest_map = (FideRatingRepository.latest_for_players(list(players))
                  if include_ratings else {})
    dataset = get_dataset()
    found = [player_to_dict(
        players[fid],
        {t: latest_map.get((fid, t)) for t in RATING_TYPES}, dataset)
        for fid in cleaned if fid in players]
    missing = [fid for fid in cleaned if fid not in players]
    return {"data": {"found": found, "missing": missing},
            "dataset": dataset}


def get_status() -> Dict:
    """Service + dataset status for operators and the Swiss client."""
    from infrastructure.repositories import FideImportJobRepository
    dataset = get_dataset()
    jobs = FideImportJobRepository.recent(limit=30)
    by_state: Dict[str, int] = {}
    for job in jobs:
        by_state[job.state] = by_state.get(job.state, 0) + 1
    last_import = None
    if dataset["period"]:
        rows = FideImportJobRepository.get_for_period(dataset["period"])
        states = {j.rating_type: j.state for j in rows}
        done = all(s == "completed" for s in states.values()) and states
        last_import = {
            "period": dataset["period"],
            "status": "success" if done else "incomplete",
            "jobs": states,
            "imported_at": dataset["imported_at"],
        }
    return {"data": {
        "ok": True,
        "players": FidePlayerRepository.count(),
        "ratings": FideRatingRepository.count(),
        "by_type": FideRatingRepository.count_by_type(),
        "periods": FideRatingRepository.periods(),
        "latest_period": dataset["period"],
        "last_import": last_import,
        "recent_jobs_by_state": by_state,
    }, "dataset": dataset}
