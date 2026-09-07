"""Dataset verification: counts + integrity checks. No framework imports.

Used by the `fide-verify` CLI and the verify stage narrative: proves the
September-2026-style baseline dimensions (player count, rating count,
per-type counts) and structural invariants (no duplicate keys, no null
IDs, no invalid types, no orphan ratings, sane period coverage).
"""
from typing import Dict, List

from sqlalchemy import func, select

from domain.models import RATING_TYPES
from infrastructure.database import db
from infrastructure.models import FidePlayerModel, FideRatingModel


def verify_data(period: str = "") -> Dict:
    """Return a verification report (never raises for data findings)."""
    player_q = select(func.count()).select_from(FidePlayerModel)
    rating_q = select(func.count()).select_from(FideRatingModel)
    if period:
        rating_q = rating_q.where(FideRatingModel.period == period)
    players = db.session.execute(player_q).scalar() or 0
    ratings = db.session.execute(rating_q).scalar() or 0

    type_q = (select(FideRatingModel.rating_type, func.count().label("n"))
              .group_by(FideRatingModel.rating_type))
    if period:
        type_q = type_q.where(FideRatingModel.period == period)
    by_type = {r[0]: r[1] for r in db.session.execute(type_q).all()}

    dup_q = (select(FideRatingModel.fide_id, FideRatingModel.period,
                    FideRatingModel.rating_type,
                    func.count().label("n"))
             .group_by(FideRatingModel.fide_id, FideRatingModel.period,
                       FideRatingModel.rating_type)
             .having(func.count() > 1).limit(10))
    if period:
        dup_q = dup_q.where(FideRatingModel.period == period)
    duplicates = [{"fide_id": r[0], "period": r[1], "rating_type": r[2],
                   "count": r[3]} for r in db.session.execute(dup_q).all()]

    empty_ids = (db.session.execute(
        select(func.count()).select_from(FidePlayerModel)
        .where((FidePlayerModel.fide_id.is_(None))
               | (FidePlayerModel.fide_id == ""))).scalar() or 0)

    invalid_types = sorted(
        t for t in
        db.session.execute(select(FideRatingModel.rating_type).distinct())
        .scalars().all() if t not in RATING_TYPES)

    orphan_q = (select(func.count()).select_from(FideRatingModel)
                .outerjoin(FidePlayerModel,
                           FideRatingModel.fide_id == FidePlayerModel.fide_id)
                .where(FidePlayerModel.fide_id.is_(None)))
    orphans = db.session.execute(orphan_q).scalar() or 0

    periods: List[str] = [
        r[0] for r in db.session.execute(
            select(FideRatingModel.period).distinct()
            .order_by(FideRatingModel.period.asc())).all()]

    ok = (not duplicates and empty_ids == 0
          and not invalid_types and orphans == 0)
    return {
        "ok": ok,
        "period": period or "all",
        "players": players,
        "ratings": ratings,
        "by_type": by_type,
        "duplicate_keys": duplicates,
        "empty_fide_ids": empty_ids,
        "invalid_rating_types": invalid_types,
        "orphan_ratings": orphans,
        "periods": periods,
    }
