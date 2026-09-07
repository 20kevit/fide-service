"""FIDE repositories. Flush-only: the application layer owns commit/rollback.

Latest-per-type reads are resolved by the DATABASE (GROUP BY max period),
never by fetching full histories into Python — the §14 performance rule.
"""
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import and_, func, select

from domain.models import ACTIVE_JOB_STATES, RATING_TYPE_ORDER
from infrastructure.database import db
from infrastructure.models import (
    FideImportJobModel, FidePlayerModel, FideRatingModel,
)


class FidePlayerRepository:
    @staticmethod
    def get_by_fide_id(fide_id: str) -> Optional[FidePlayerModel]:
        return db.session.get(FidePlayerModel, fide_id)

    @staticmethod
    def get_by_fide_ids(fide_ids: List[str]) -> Dict[str, FidePlayerModel]:
        """Bulk fetch mapped by fide_id. Missing IDs are simply absent."""
        if not fide_ids:
            return {}
        rows = db.session.execute(
            select(FidePlayerModel).where(
                FidePlayerModel.fide_id.in_(fide_ids))
        ).scalars().all()
        return {p.fide_id: p for p in rows}

    @staticmethod
    def save(player: FidePlayerModel) -> FidePlayerModel:
        db.session.add(player)
        db.session.flush()
        return player

    @staticmethod
    def count() -> int:
        return db.session.execute(
            select(func.count()).select_from(FidePlayerModel)).scalar() or 0

    @staticmethod
    def search(query: str, federation: str = "",
               limit: int = 20) -> List[FidePlayerModel]:
        """Bounded search: numeric q → fide_id prefix; else ALL name terms
        (case-insensitive substring). Optional exact federation filter."""
        stmt = select(FidePlayerModel)
        if query:
            if query.isdigit():
                stmt = stmt.where(
                    FidePlayerModel.fide_id.like(query + "%"))
            else:
                for term in query.split():
                    if term:
                        stmt = stmt.where(
                            FidePlayerModel.name.ilike("%" + term + "%"))
        if federation:
            stmt = stmt.where(FidePlayerModel.federation == federation)
        stmt = stmt.order_by(FidePlayerModel.fide_id.asc()).limit(limit)
        return list(db.session.execute(stmt).scalars().all())


class FideRatingRepository:
    @staticmethod
    def get(fide_id: str, period: str,
            rating_type: str) -> Optional[FideRatingModel]:
        return db.session.execute(
            select(FideRatingModel).where(and_(
                FideRatingModel.fide_id == fide_id,
                FideRatingModel.period == period,
                FideRatingModel.rating_type == rating_type))
        ).scalars().first()

    @staticmethod
    def save(rating: FideRatingModel) -> FideRatingModel:
        db.session.add(rating)
        db.session.flush()
        return rating

    @staticmethod
    def _latest_rows(fide_ids: List[str]):
        """Latest-period row per (fide_id, rating_type), in ONE query."""
        if not fide_ids:
            return []
        maxq = (
            select(FideRatingModel.fide_id, FideRatingModel.rating_type,
                   func.max(FideRatingModel.period).label("max_period"))
            .where(FideRatingModel.fide_id.in_(fide_ids))
            .group_by(FideRatingModel.fide_id, FideRatingModel.rating_type)
            .subquery()
        )
        stmt = (
            select(FideRatingModel)
            .join(maxq, and_(
                FideRatingModel.fide_id == maxq.c.fide_id,
                FideRatingModel.rating_type == maxq.c.rating_type,
                FideRatingModel.period == maxq.c.max_period))
        )
        return db.session.execute(stmt).scalars().all()

    @staticmethod
    def latest_for_player(fide_id: str) -> Dict[str, Optional[FideRatingModel]]:
        """{standard, rapid, blitz} latest rows (None when absent)."""
        out: Dict[str, Optional[FideRatingModel]] = {
            "standard": None, "rapid": None, "blitz": None}
        if not fide_id:
            return out
        for row in FideRatingRepository._latest_rows([fide_id]):
            out[row.rating_type] = row
        return out

    @staticmethod
    def latest_for_players(
            fide_ids: List[str]) -> Dict[Tuple[str, str], FideRatingModel]:
        """Bulk latest: {(fide_id, rating_type): row}, one query (no N+1)."""
        return {(r.fide_id, r.rating_type): r
                for r in FideRatingRepository._latest_rows(fide_ids)}

    @staticmethod
    def history(fide_id: str, rating_type: str = "",
                period_from: str = "", period_to: str = "",
                limit: int = 200, offset: int = 0) -> List[FideRatingModel]:
        """Ascending history with optional type/period-range filters."""
        if not fide_id:
            return []
        stmt = (select(FideRatingModel)
                .where(FideRatingModel.fide_id == fide_id))
        if rating_type:
            stmt = stmt.where(FideRatingModel.rating_type == rating_type)
        if period_from:
            stmt = stmt.where(FideRatingModel.period >= period_from)
        if period_to:
            stmt = stmt.where(FideRatingModel.period <= period_to)
        stmt = (stmt.order_by(FideRatingModel.period.asc(),
                              FideRatingModel.rating_type.asc())
                .limit(limit).offset(offset))
        return list(db.session.execute(stmt).scalars().all())

    @staticmethod
    def periods() -> List[str]:
        """Distinct dataset periods, ascending."""
        rows = db.session.execute(
            select(FideRatingModel.period).distinct()
            .order_by(FideRatingModel.period.asc())).all()
        return [r[0] for r in rows]

    @staticmethod
    def latest_period() -> str:
        row = db.session.execute(
            select(func.max(FideRatingModel.period))).scalar()
        return row or ""

    @staticmethod
    def count() -> int:
        return db.session.execute(
            select(func.count()).select_from(FideRatingModel)).scalar() or 0

    @staticmethod
    def count_by_type(period: str = "") -> Dict[str, int]:
        stmt = (select(FideRatingModel.rating_type,
                       func.count().label("n"))
                .group_by(FideRatingModel.rating_type))
        if period:
            stmt = stmt.where(FideRatingModel.period == period)
        return {r[0]: r[1] for r in db.session.execute(stmt).all()}

    @staticmethod
    def prune_history(keep_period: str, rating_type: str,
                      keep_federations: List[str]) -> int:
        """Delete pre-``keep_period`` snapshots of one type for players
        OUTSIDE ``keep_federations``. Empty keep-list keeps everything.
        Returns deleted row count. This is what makes history IRI-only
        while identity + current ratings stay global.
        """
        if not keep_federations:
            return 0
        keep_ids = (select(FidePlayerModel.fide_id)
                    .where(FidePlayerModel.federation.in_(keep_federations)))
        stmt = (FideRatingModel.__table__.delete().where(and_(
            FideRatingModel.rating_type == rating_type,
            FideRatingModel.period < keep_period,
            ~FideRatingModel.fide_id.in_(keep_ids))))
        result = db.session.execute(stmt)
        db.session.flush()
        return result.rowcount or 0


class FideImportJobRepository:
    """Flush-only persistence for worker jobs (service owns transactions)."""

    @staticmethod
    def get(period: str, rating_type: str) -> Optional[FideImportJobModel]:
        return db.session.execute(
            select(FideImportJobModel).where(and_(
                FideImportJobModel.period == period,
                FideImportJobModel.rating_type == rating_type))
        ).scalars().first()

    @staticmethod
    def get_for_period(period: str) -> List[FideImportJobModel]:
        return list(db.session.execute(
            select(FideImportJobModel)
            .where(FideImportJobModel.period == period)
            .order_by(FideImportJobModel.id.asc())).scalars().all())

    @staticmethod
    def recent(limit: int = 20) -> List[FideImportJobModel]:
        return list(db.session.execute(
            select(FideImportJobModel)
            .order_by(FideImportJobModel.period.desc(),
                      FideImportJobModel.id.asc())
            .limit(limit)).scalars().all())

    @staticmethod
    def save(job: FideImportJobModel) -> FideImportJobModel:
        db.session.add(job)
        db.session.flush()
        return job

    @staticmethod
    def fail_stale_jobs(period: str,
                        timeout_seconds: int) -> List[FideImportJobModel]:
        """Mark active jobs with expired (or missing) heartbeat as failed.

        Only identifies stale work — retry stays explicit. Flush-only.
        """
        cutoff = datetime.utcnow() - timedelta(seconds=timeout_seconds)
        stmt = (select(FideImportJobModel)
                .where(FideImportJobModel.period == period)
                .where(FideImportJobModel.state.in_(ACTIVE_JOB_STATES)))
        try:
            stmt = stmt.with_for_update()
        except Exception:
            pass  # SQLite: row locks unsupported; single-worker safe
        stale = db.session.execute(stmt).scalars().all()
        failed = []
        for job in stale:
            if job.heartbeat_at is not None and job.heartbeat_at >= cutoff:
                continue
            job.state = "failed"
            job.error_code = "WORKER_STALE"
            job.error_message = (
                "Worker stopped without heartbeat. "
                "This step can be safely retried explicitly.")
            job.completed_at = datetime.utcnow()
            db.session.add(job)
            failed.append(job)
        db.session.flush()
        return failed

    @staticmethod
    def claim_next_job(period: str, timeout_seconds: int):
        """Evaluate + atomically claim the next eligible step.

        Returns (job, reason): 'claimed' | 'busy' | 'stale_present' |
        'blocked' | 'failed_present' | 'nothing_pending'. Flush-only:
        the SERVICE commits on 'claimed' and rolls back otherwise.
        """
        cutoff = datetime.utcnow() - timedelta(seconds=timeout_seconds)
        stmt = (select(FideImportJobModel)
                .where(FideImportJobModel.period == period))
        try:
            stmt = stmt.with_for_update()
        except Exception:
            pass
        rows = db.session.execute(stmt).scalars().all()
        by_type = {j.rating_type: j for j in rows}
        for rating_type in RATING_TYPE_ORDER:
            job = by_type.get(rating_type)
            if job is None or job.state == "completed":
                continue
            if job.state in ACTIVE_JOB_STATES:
                if job.heartbeat_at is not None and job.heartbeat_at >= cutoff:
                    return None, "busy"
                return None, "stale_present"
            if job.state == "failed":
                return None, "failed_present"
            if job.state == "pending":
                blocked = any(
                    (by_type.get(prev) is None
                     or by_type[prev].state != "completed")
                    for prev in RATING_TYPE_ORDER[
                        :RATING_TYPE_ORDER.index(rating_type)])
                if blocked:
                    return None, "blocked"
                now = datetime.utcnow()
                job.state = "downloading"
                job.started_at = job.started_at or now
                job.heartbeat_at = now
                job.records_processed = 0
                job.total_records = None
                job.error_code = None
                job.error_message = None
                db.session.add(job)
                db.session.flush()
                return job, "claimed"
        return None, "nothing_pending"
